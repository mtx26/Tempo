"""Conversational assistant on a REAL PostgreSQL with a scripted Gemini client.

The scripted client stands in for Gemini on Vertex AI (no network, no cost): it proves the
harness runs the operations the model asks for, returns their real results, keeps going until
the model answers, and falls back to the deterministic parser when the LLM is unavailable."""

import logging
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import types
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tempo.api import assistant as conversation_routes
from tempo.api.app import create_app
from tempo.assistant.agent import SYSTEM, GeminiChat, LLMUnavailableError, ToolLoopAssistant
from tempo.assistant.catalog import CATALOG, NoInput, OpContext, Operation, OpError, run_operation
from tempo.config import Settings
from tempo.db.models import (
    ActivityRow,
    Base,
    ConversationTurnRow,
    NotificationRow,
    ProposalRow,
    SettingRow,
    StudyTaskRow,
)
from tempo.db.store import active_version, replan_request
from tests.integration.test_planning_service import NOW, TZ, seed
from tests.test_engine import FakeRouting

pytestmark = pytest.mark.integration
TOKEN = "Hq8Wm2Zr5Tx9Lb3Nc7Vd1Kf4Gs6Pj0YeUa"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def tool_use(name: str, arguments: dict[str, Any]) -> types.Part:
    return types.Part(
        function_call=types.FunctionCall(id=f"fc_{uuid.uuid4().hex[:8]}", name=name, args=arguments)
    )


def say(words: str) -> types.Part:
    return types.Part.from_text(text=words)


def response(*parts: types.Part, finish: str = "STOP") -> SimpleNamespace:
    candidate = SimpleNamespace(
        content=types.Content(role="model", parts=list(parts)),
        finish_reason=types.FinishReason(finish),
    )
    return SimpleNamespace(candidates=[candidate], prompt_feedback=None)


def blocked() -> SimpleNamespace:
    return SimpleNamespace(candidates=[], prompt_feedback=SimpleNamespace(block_reason="SAFETY"))


class ScriptedModels:
    """Returns scripted responses in order and records every request."""

    def __init__(self, *script: SimpleNamespace | Exception) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append({**kwargs, "contents": list(kwargs["contents"])})  # snapshot
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def assistant(models: ScriptedModels, **options: Any) -> ToolLoopAssistant:
    client = SimpleNamespace(models=models)
    return ToolLoopAssistant(GeminiChat(client), model="gemini-model-under-test", **options)


def tool_results(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Function responses sent back in the last user turn, as plain dicts."""
    last = request["contents"][-1]
    assert last.role == "user"
    return [p.function_response.response for p in last.parts if p.function_response]


def outage() -> httpx.ConnectError:
    return httpx.ConnectError("Vertex AI unreachable")


def test_solre_workday_survives_clarification(
    sessions: sessionmaker[Session], database_url: str
) -> None:
    from tempo.db.models import PlaceRow

    now = datetime(2026, 11, 27, 21, 23, tzinfo=TZ)
    work = {
        "title": "Travail avec mon père à Solre-sur-Sambre",
        "scheduling": "fixed",
        "start": "2026-11-28T00:00",
        "all_day": True,
        "place_id": "florne",
        "remote": True,
    }
    with sessions.begin() as session:
        seed(session)
        session.add(PlaceRow(id="florne", name="Maison de Florne", latitude=50.3, longitude=4.2))
        ctx = OpContext(session, now, TZ, "assistant", FakeRouting())
        dinner = run_operation(
            ctx,
            "create_event",
            {
                "title": "Souper chez ma copine",
                "scheduling": "fixed",
                "remote": True,
                "start": "2026-11-28T18:00",
                "duration_minutes": 360,
            },
        )["result"]["activity"]["id"]
        training = run_operation(
            ctx,
            "create_event",
            {
                "title": "Endurance Fondamentale",
                "kind": "training",
                "scheduling": "flexible",
                "window_start": "2026-11-28T11:15",
                "window_end": "2026-11-28T12:15",
                "duration_minutes": 60,
            },
        )["result"]["activity"]["id"]
        run_operation(ctx, "plan_now", {})
        training_row = session.get(ActivityRow, uuid.UUID(training))
        assert training_row is not None
        assert training_row.planned_start_at == datetime(2026, 11, 28, 11, 15, tzinfo=TZ)
    llm = ScriptedModels(
        response(tool_use("create_event", work)),
        response(tool_use("plan_now", {})),
        response(
            say("J'ai ajouté le travail."),
            tool_use(
                "clarify_request",
                {
                    "question": "Voulez-vous garder Endurance Fondamentale à 11 h 15 ?",
                    "keep_activities": [dinner],
                },
            ),
        ),
    )
    app = create_app(Settings(api_token=TOKEN, database_url=database_url))
    app.state.assistant = assistant(llm)
    app.state.routing = FakeRouting()
    app.state.clock = lambda: now
    with TestClient(app) as api:
        initial = api.post(
            "/v1/conversation",
            headers=AUTH,
            json={
                "text": (
                    "Demain, je travaille toute la journée avec mon père dans la maison de "
                    "Florne à Solre-sur-Sambre. Je viens en voiture, donc pas besoin de noter "
                    "le transport."
                )
            },
        ).json()
        assert initial["status"] == "clarification" and not initial["changed"]
        assert "J'ai ajouté" not in initial["reply"]
        draft_id = initial["draft_id"]
        with sessions() as session:
            draft = session.get(ProposalRow, uuid.UUID(draft_id))
            assert draft is not None and draft.details["changes"][0]["arguments"] == work
            assert (
                session.scalar(select(ActivityRow).where(ActivityRow.title == work["title"]))
                is None
            )
        cancellation = {
            "name": "cancel_event",
            "arguments": {"activity_id": training, "scope": "occurrence"},
        }
        # The next turn uses a fresh assistant and reads the prepared steps from PostgreSQL.
        llm.script.append(
            response(
                tool_use(
                    "resume_request",
                    {
                        "draft_id": draft_id,
                        "changes": [cancellation],
                    },
                )
            )
        )
        app.state.assistant = assistant(llm)
        preview = api.post(
            "/v1/conversation",
            headers=AUTH,
            json={"text": "L'entraînement annule-le et oui je vais toujours chez ma copine."},
        ).json()
        assert preview["proposal_id"] == draft_id and not preview["changed"], preview
        for label in (
            "Solre-sur-Sambre",
            "Annuler « Endurance Fondamentale »",
            "Souper",
            "18:00",
            "Aucun trajet ajouté",
            "chevauchement",
        ):
            assert label in preview["reply"]
        with sessions() as session:
            assert (
                session.scalar(select(ActivityRow).where(ActivityRow.title == work["title"]))
                is None
            )
            row = session.get(ProposalRow, uuid.UUID(draft_id))
            assert row is not None
            work_step, cancel_step = row.details["plan_changes"]
            assert work_step["arguments"] == work and cancel_step == cancellation
        approval = api.post(
            "/v1/conversation", headers=AUTH, json={"text": f"confirme {draft_id[:8]}"}
        ).json()
        assert approval["changed"], approval
        duplicate = api.post(
            "/v1/conversation", headers=AUTH, json={"text": f"confirme {draft_id[:8]}"}
        ).json()
        assert not duplicate["changed"]
    with sessions() as session:
        added = session.scalars(select(ActivityRow).where(ActivityRow.title == work["title"])).one()
        assert added.remote and added.all_day and added.place_id == "florne"
        cancelled = session.get(ActivityRow, uuid.UUID(training))
        saved = session.get(ActivityRow, uuid.UUID(dinner))
        assert cancelled is not None and cancelled.status == "cancelled"
        assert saved is not None and saved.status == "active"
        assert saved.earliest_start_at == datetime(2026, 11, 28, 18, tzinfo=TZ)
        plan = active_version(session)
        assert plan is not None
        assert all(journey["event_title"] != work["title"] for journey in plan.summary["travel"])


@pytest.mark.parametrize("with_saved_place", [False, True])
def test_revise_workday_preview_after_question_about_hours(
    session: Session, with_saved_place: bool
) -> None:
    from tempo.db.models import PlaceRow

    seed(session)
    now = datetime(2026, 11, 27, 21, 23, tzinfo=TZ)
    ctx = OpContext(session, now, TZ, "assistant", FakeRouting(), user_id="student")
    title = "Travail à Solre-sur-Sambre"
    work = {
        "title": title,
        "scheduling": "fixed",
        "all_day": True,
        "start": "2026-11-28T00:00",
        "remote": True,
    }
    changes = []
    if with_saved_place:
        changes.append(
            {
                "name": "upsert_place",
                "ref": "place",
                "arguments": {
                    "place_id": "solre_sur_sambre",
                    "name": "Solre-sur-Sambre",
                    "latitude": 50.3,
                    "longitude": 4.2,
                },
            }
        )
        work["place_id"] = "solre_sur_sambre"
    changes.append({"name": "create_event", "arguments": work})
    first_turn = (
        [response(tool_use("preview_plan_changes", {"changes": changes}))]
        if with_saved_place
        else [response(tool_use("create_event", work)), response(say("Voici le résultat."))]
    )
    script = ScriptedModels(
        *first_turn, response(say("Pour le 28 novembre, de minuit au minuit suivant."))
    )
    agent = assistant(script)
    original = agent.reply(ctx, "Demain je travaille toute la journée à Solre, sans trajet.", [])
    original_id = original.proposal_id
    assert original_id is not None
    assert "00:00 à 00:00 le 29/11" in original.text
    question = agent.reply(
        ctx, "Attends, journée entière c'est de quelle heure à quelle heure ?", []
    )
    assert question.status == "informative" and question.draft_id is None
    original_row = session.get(ProposalRow, uuid.UUID(original_id))
    assert original_row is not None and original_row.status == "pending"

    edit: dict[str, Any] = {
        "all_day": False,
        "start": "2026-11-28T08:00",
        "end": "2026-11-28T17:00",
    }
    if with_saved_place:
        edit["place_id"] = {"is_ref": True, "ref": "place"}
        script.script.append(
            response(
                tool_use(
                    "resume_request",
                    {
                        "draft_id": original_id,
                        "edits": [
                            {
                                "step": len(changes),
                                "arguments": {
                                    **edit,
                                    "place_id": {"is_ref": True, "ref": "missing"},
                                },
                            }
                        ],
                    },
                )
            )
        )
        invalid = agent.reply(ctx, "De 8 à 17 h.", [])
        assert invalid.status == "interrupted" and invalid.proposal_id is None
        assert "validation error" not in invalid.text and "pydantic.dev" not in invalid.text
        assert original_row.status == "pending"
    script.script.append(
        response(
            tool_use(
                "resume_request",
                {
                    "draft_id": original_id,
                    "edits": [{"step": len(changes), "arguments": edit}],
                },
            )
        )
    )
    revised = agent.reply(ctx, "Oui, genre 8 à 17 h.", [])
    assert original_id in script.requests[-1]["contents"][-1].parts[0].text
    new_id = revised.proposal_id
    assert new_id is not None and new_id != original_id, revised.text
    assert "08:00 à 17:00" in revised.text and "Aucun trajet ajouté" in revised.text
    assert original_row.status == "obsolete"
    assert (
        ctx.changed == []
        and session.scalar(select(ActivityRow).where(ActivityRow.title == title)) is None
    )
    assert session.get(PlaceRow, "solre_sur_sambre") is None
    former = run_operation(ctx, "decide_proposal", {"proposal_id": original_id, "approve": True})
    assert not former["ok"]
    approval = run_operation(ctx, "decide_proposal", {"proposal_id": new_id, "approve": True})
    assert approval["ok"], approval
    activity = session.scalars(select(ActivityRow).where(ActivityRow.title == title)).one()
    assert activity.remote and not activity.all_day
    assert activity.earliest_start_at == datetime(2026, 11, 28, 8, tzinfo=TZ)
    assert activity.latest_end_at == datetime(2026, 11, 28, 17, tzinfo=TZ)
    assert activity.place_id == ("solre_sur_sambre" if with_saved_place else None)
    assert (session.get(PlaceRow, "solre_sur_sambre") is not None) == with_saved_place


def test_compound_request_previews_real_results_before_applying(session: Session) -> None:
    seed(session)
    llm = ScriptedModels(
        response(say("Je regarde."), tool_use("get_context", {})),
        response(
            tool_use(
                "create_task",
                {
                    "title": "Physique",
                    "estimated_minutes": 900,
                    "splittable": True,
                    "target_date": "2026-12-11",
                },
            ),
            tool_use(
                "create_event",
                {
                    "title": "Dentiste",
                    "scheduling": "fixed",
                    "start": "2026-12-10T21:00",
                    "duration_minutes": 30,
                    "remote": True,
                },
            ),
        ),
        response(tool_use("plan_now", {})),
        response(say("C'est noté : 15 h de physique et le dentiste jeudi.")),
    )
    ctx = OpContext(
        session=session, now=NOW, tz=TZ, actor="assistant", routing=FakeRouting(), user_id="student"
    )
    reply = assistant(llm).reply(ctx, "15 h de physique avant vendredi et dentiste jeudi 21h", [])
    assert reply.text.startswith("Aperçu à confirmer")
    assert [o["name"] for o in reply.operations] == [
        "get_context",
        "create_task",
        "create_event",
        "plan_now",
    ]
    assert all(o["ok"] for o in reply.operations)
    # the two parallel calls were answered in ONE user message, with real results
    results = tool_results(llm.requests[2])
    assert len(results) == 2 and all(r["ok"] for r in results)
    plan = tool_results(llm.requests[3])[0]
    assert plan["result"]["published"] is True
    assert session.scalar(select(StudyTaskRow).where(StudyTaskRow.subject == "Physique")) is None
    assert replan_request(session) is None
    assert reply.proposal_id is not None
    assert ctx.changed == []
    decision = run_operation(
        ctx, "decide_proposal", {"proposal_id": reply.proposal_id, "approve": True}
    )
    assert decision["result"]["proposal"]["status"] == "approved"
    assert session.scalars(select(StudyTaskRow).where(StudyTaskRow.subject == "Physique")).one()
    # the request carries the stable cached system prompt, tools and today's date
    first = llm.requests[0]
    assert first["config"].system_instruction == SYSTEM
    declarations = first["config"].tools[0].function_declarations
    assert any(d.name == "create_event" for d in declarations)
    assert first["config"].automatic_function_calling.disable is True
    # identical prefix on every call (instructions, then tools): Gemini's implicit cache hits
    for later in llm.requests[1:]:
        assert later["config"].system_instruction == first["config"].system_instruction
        assert later["config"].tools == first["config"].tools
    assert "2026" in first["contents"][-1].parts[0].text
    assert ctx.changed == ["create_task", "create_event", "decide_proposal"]


def test_failed_operation_is_reported_to_the_model_and_rolled_back(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    seed(session)
    llm = ScriptedModels(
        response(
            tool_use(
                "create_event",
                {
                    "title": "Sortie",
                    "scheduling": "flexible",
                    "window_start": "2026-12-11T18:00",
                    "window_end": "2026-12-11T23:00",
                },
            ),
        ),
        response(say("Combien de temps dure la sortie ?")),
    )
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant")
    with caplog.at_level(logging.INFO, logger="tempo.ai.llm"):
        reply = assistant(llm).reply(ctx, "ajoute une sortie vendredi soir", [])
    usage = [r.getMessage() for r in caplog.records if "llm usage" in r.getMessage()]
    assert len(usage) == 2 and all("purpose=conversation" in line for line in usage)
    assert not any("sortie" in line.lower() for line in usage)  # no content in the log
    assert reply.text.endswith("?")
    result = tool_results(llm.requests[1])[0]
    assert not result["ok"] and "durée inconnue" in result["error"]
    assert session.scalars(select(ActivityRow).where(ActivityRow.title == "Sortie")).first() is None
    assert ctx.changed == []


def test_unavailable_llm_raises_to_be_reported(session: Session) -> None:
    llm = ScriptedModels(outage())
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant")
    with pytest.raises(LLMUnavailableError):
        assistant(llm).reply(ctx, "bonjour", [])
    refused = ScriptedModels(blocked())
    with pytest.raises(LLMUnavailableError):
        assistant(refused).reply(ctx, "bonjour", [])


def test_runaway_loop_is_bounded(session: Session) -> None:
    seed(session)
    llm = ScriptedModels(*[response(tool_use("get_summary", {})) for _ in range(20)])
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant")
    reply = assistant(llm).reply(ctx, "boucle", [])
    assert "trop longue" in reply.text and len(llm.requests) == 16


@pytest.fixture
def sessions(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


def test_conversation_route_keeps_context_and_reports_outages(
    sessions: sessionmaker[Session], database_url: str
) -> None:
    with sessions() as s:
        seed(s)
        s.commit()
    llm = ScriptedModels(
        response(tool_use("list_tasks", {})),
        response(say("Vous avez une tâche : maths.")),
        response(say("Elle compte 300 minutes.")),
        outage(),
    )
    app = create_app(Settings(api_token=TOKEN, database_url=database_url))
    with TestClient(app) as api:
        app.state.assistant = assistant(llm)
        first = api.post(
            "/v1/conversation",
            headers=AUTH,
            json={"text": "quelles tâches ?", "conversation_id": "phone"},
        )
        assert first.status_code == 200, first.text
        assert first.json()["used_llm"] and first.json()["reply"] == "Vous avez une tâche : maths."
        second = api.post(
            "/v1/conversation",
            headers=AUTH,
            json={"text": "combien de temps pour elle ?", "conversation_id": "phone"},
        )
        assert second.json()["reply"] == "Elle compte 300 minutes."
        # the reference "elle" is resolvable: previous turns were sent back
        history = llm.requests[2]["contents"]
        assert history[0].role == "user" and history[0].parts[0].text == "quelles tâches ?"
        assert history[1].role == "model"
        # Gemini outage: reported as is, nothing changed, nothing invented
        third = api.post(
            "/v1/conversation",
            headers=AUTH,
            json={"text": "combien d'heures d'étude il me reste ?"},
        )
        assert third.status_code == 200
        assert third.json()["used_llm"] is False and third.json()["error"] == "llm_unavailable"
        assert third.json()["changed"] is False
        assert api.post("/v1/conversation", json={"text": "x"}).status_code == 401
    with sessions() as s:
        # 2 LLM exchanges kept as context; the failed one is not a conversation turn
        assert len(list(s.scalars(select(ConversationTurnRow)))) == 4


def test_operations_api_catalogue_and_idempotency(
    sessions: sessionmaker[Session], database_url: str
) -> None:
    with sessions() as s:
        seed(s)
        s.commit()
    with TestClient(create_app(Settings(api_token=TOKEN, database_url=database_url))) as api:
        catalogue = api.get("/v1/operations", headers=AUTH).json()
        names = {op["name"] for op in catalogue}
        assert {"create_event", "log_work", "decide_proposal", "search_journey"} <= names
        body = {"title": "Rapport", "kind": "personal", "estimated_minutes": 60, "splittable": True}
        headers = {**AUTH, "Idempotency-Key": "abc-1"}
        one = api.post("/v1/operations/create_task", headers=headers, json=body)
        two = api.post("/v1/operations/create_task", headers=headers, json=body)
        assert one.status_code == 200 and one.json() == two.json()
        conflict = api.post(
            "/v1/operations/create_task", headers=headers, json={**body, "title": "Autre"}
        )
        assert conflict.status_code == 409
        bad = api.post("/v1/operations/create_task", headers=AUTH, json={"title": ""})
        assert bad.status_code == 422
        assert api.post("/v1/operations/nope", headers=AUTH, json={}).status_code == 404
    with sessions() as s:
        assert (
            len(list(s.scalars(select(StudyTaskRow).where(StudyTaskRow.subject == "Rapport")))) == 1
        )
    _ = datetime.now(UTC)


def test_concurrent_replans_never_overwrite_each_other(sessions: sessionmaker[Session]) -> None:
    """B13: API and worker re-planning at the same time are serialised (advisory lock):
    exactly one active version remains and the second plans on top of the first."""
    import threading

    from tempo.db.models import ScheduleVersion
    from tempo.planning.service import replan

    with sessions() as s:
        seed(s)
        s.commit()
    errors: list[BaseException] = []

    def run(reason: str) -> None:
        try:
            with sessions() as s:
                replan(s, FakeRouting(), NOW, TZ, reason=reason)
                s.commit()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"r{i}",)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not errors, errors
    with sessions() as s:
        states = [v.state for v in s.scalars(select(ScheduleVersion))]
    assert states.count("active") == 1 and states.count("superseded") == 2


def test_llm_outage_after_a_change_rolls_back_the_turn(session: Session) -> None:
    """An interrupted Gemini turn leaves no business change or replan request."""
    seed(session)
    llm = ScriptedModels(
        response(tool_use("create_task", {"title": "Économie", "estimated_minutes": 60})),
        outage(),
    )
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant")
    with pytest.raises(LLMUnavailableError):
        assistant(llm).reply(ctx, "ajoute 1 h d'économie", [])
    assert ctx.changed == []
    assert session.scalar(select(StudyTaskRow).where(StudyTaskRow.subject == "Économie")) is None


def test_conversation_replays_only_the_same_idempotent_request(
    sessions: sessionmaker[Session], database_url: str
) -> None:
    scripted = ScriptedModels(response(say("Bonjour.")))
    app = create_app(Settings(api_token=TOKEN, database_url=database_url))
    app.state.assistant = assistant(scripted)
    headers = {**AUTH, "Idempotency-Key": "same-voice-message"}
    with TestClient(app) as api:
        first = api.post("/v1/conversation", headers=headers, json={"text": "Bonjour"})
        retry = api.post("/v1/conversation", headers=headers, json={"text": "Bonjour"})
        other = api.post("/v1/conversation", headers=headers, json={"text": "Au revoir"})
    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json()
    assert other.status_code == 409
    assert len(scripted.requests) == 1


def test_time_budget_rolls_back_the_whole_turn(session: Session) -> None:
    seed(session)
    ticks = iter([0.0, 0.0, 58.0, 58.0])
    llm = ScriptedModels(
        response(
            tool_use("create_task", {"title": "Bio", "estimated_minutes": 30, "splittable": True})
        ),
        response(say("jamais atteint")),
    )
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant")
    agent = assistant(llm, budget_seconds=60, clock=lambda: next(ticks))
    reply = agent.reply(ctx, "ajoute bio", [])
    assert "manque de temps" in reply.text and "Aucune modification" in reply.text
    assert ctx.changed == [] and replan_request(session) is None
    assert session.scalar(select(StudyTaskRow).where(StudyTaskRow.subject == "Bio")) is None
    assert len(llm.requests) == 1
    assert llm.requests[0]["config"].http_options.timeout == 60_000


@pytest.mark.parametrize("failure", ["invalid", "truncated", "limit"])
def test_interrupted_compound_turn_never_commits_a_prefix(session: Session, failure: str) -> None:
    seed(session)
    first = response(
        tool_use("create_task", {"title": "Lot A", "estimated_minutes": 45, "splittable": True})
    )
    following = {
        "invalid": [response(tool_use("update_task", {"task_id": "faux"}))],
        "truncated": [response(tool_use("create_task", {"title": "Lot B"}), finish="MAX_TOKENS")],
        "limit": [response(tool_use("get_summary", {}))] * 15,
    }
    ctx = OpContext(
        session=session, now=NOW, tz=TZ, actor="assistant", routing=FakeRouting(), user_id="a"
    )
    result = assistant(ScriptedModels(first, *following[failure])).reply(ctx, "modifie le lot", [])
    assert "Aucune modification" in result.text
    assert session.scalar(select(StudyTaskRow).where(StudyTaskRow.subject == "Lot A")) is None
    assert ctx.changed == [] and replan_request(session) is None
    assert result.status == "interrupted" and result.draft_id is not None
    resumed = assistant(
        ScriptedModels(response(tool_use("resume_request", {"draft_id": result.draft_id})))
    ).reply(ctx, "reprends uniquement la tâche Lot A", [])
    assert resumed.proposal_id == result.draft_id
    row = session.get(ProposalRow, uuid.UUID(resumed.proposal_id))
    assert row is not None
    assert [step["arguments"]["title"] for step in row.details["plan_changes"]] == ["Lot A"]


@pytest.mark.parametrize("decision", ["approved", "rejected", "expired", "stale"])
def test_proposal_api_binds_user_revision_arguments_and_expiry(
    sessions: sessionmaker[Session], database_url: str, decision: str
) -> None:
    with sessions.begin() as session:
        seed(session)
    changes = {
        "changes": [
            {
                "name": "create_task",
                "arguments": {"title": "Lot approuvé", "estimated_minutes": 60, "splittable": True},
            },
        ]
    }
    script = ScriptedModels(response(tool_use("preview_plan_changes", changes)))
    app = create_app(Settings(api_token=TOKEN, database_url=database_url))
    app.state.assistant = assistant(script)
    app.state.routing = FakeRouting()
    app.state.clock = lambda: NOW
    approval_key = {**AUTH, "Idempotency-Key": "same-approval"}
    with TestClient(app) as api:
        preview = api.post(
            "/v1/conversation", headers=AUTH, json={"text": "simule", "user_id": "a"}
        )
        assert preview.status_code == 200 and not preview.json()["changed"]
        proposal_id = preview.json()["proposal_id"]
        assert proposal_id[:8] in preview.json()["reply"]
        with sessions() as session:
            row = session.get(ProposalRow, uuid.UUID(proposal_id))
            assert row is not None and row.details["plan_changes"] == changes["changes"]
            assert (
                session.scalar(select(StudyTaskRow).where(StudyTaskRow.subject == "Lot approuvé"))
                is None
            )
            assert replan_request(session) is None
        app.state.assistant = None
        response_other = api.post(
            "/v1/conversation",
            headers=AUTH,
            json={"text": f"confirme {proposal_id[:8]}", "user_id": "b"},
        )
        assert not response_other.json()["changed"]
        if decision == "expired":
            app.state.clock = lambda: NOW + timedelta(minutes=16)
        if decision == "stale":
            assert (
                api.post(
                    "/v1/operations/create_task",
                    headers=AUTH,
                    json={"title": "Autre tâche", "estimated_minutes": 60, "splittable": True},
                ).status_code
                == 200
            )
        command = "refuse" if decision == "rejected" else "confirme"
        payload = {"text": f"{command} {proposal_id[:8]}", "user_id": "a"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [
                pool.submit(api.post, "/v1/conversation", headers=approval_key, json=payload)
                for _ in range(2)
            ]
            first, retry = [call.result(timeout=20) for call in calls]
        assert first.json() == retry.json()
        assert first.json()["used_llm"] is False
        assert first.json()["changed"] == (decision in {"approved", "rejected"})
    with sessions() as session:
        tasks = list(
            session.scalars(select(StudyTaskRow).where(StudyTaskRow.subject == "Lot approuvé"))
        )
        assert len(tasks) == int(decision == "approved")
        row = session.get(ProposalRow, uuid.UUID(proposal_id))
        assert row is not None
        assert row.status == ("obsolete" if decision in {"expired", "stale"} else decision)
    assert len(script.requests) == 1


def test_simulator_cannot_call_a_new_external_mutation(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[str] = []

    def external_write(ctx: OpContext, arguments: NoInput) -> dict[str, Any]:
        writes.append("sent")
        return {}

    op = Operation("external_write", "External", NoInput, external_write, True)
    monkeypatch.setitem(CATALOG, op.name, op)
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant", routing=FakeRouting())
    outcome = run_operation(
        ctx, "preview_plan_changes", {"changes": [{"name": op.name, "arguments": {}}]}
    )
    assert not outcome["ok"] and "arguments invalides" in outcome["error"]
    assert writes == []


def test_approval_rolls_back_the_prefix_on_a_business_failure(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(session)
    changes = [
        {
            "name": "create_task",
            "arguments": {"title": "Préfixe", "estimated_minutes": 60, "splittable": True},
        },
        {
            "name": "create_event",
            "arguments": {
                "title": "Dernière étape",
                "scheduling": "fixed",
                "start": "2026-12-11T21:00",
                "duration_minutes": 30,
                "remote": True,
            },
        },
    ]
    ctx = OpContext(
        session=session, now=NOW, tz=TZ, actor="assistant", routing=FakeRouting(), user_id="a"
    )
    first = assistant(
        ScriptedModels(
            response(
                tool_use(
                    "clarify_request",
                    {
                        "changes": changes,
                        "question": "Gardez-vous deux étapes ?",
                    },
                )
            )
        )
    ).reply(ctx, "Prépare le lot", [])
    assert first.draft_id is not None
    proposal = (
        assistant(
            ScriptedModels(
                response(
                    tool_use(
                        "resume_request",
                        {
                            "draft_id": first.draft_id,
                        },
                    )
                )
            )
        )
        .reply(ctx, "Oui, les deux.", [])
        .proposal_id
    )
    assert proposal is not None

    def invalid(ctx: OpContext, body: Any) -> dict[str, Any]:
        raise OpError("La deuxième étape n'est plus faisable")

    monkeypatch.setitem(CATALOG, "create_event", replace(CATALOG["create_event"], handler=invalid))
    outcome = run_operation(ctx, "decide_proposal", {"proposal_id": proposal, "approve": True})
    assert not outcome["ok"] and "étape 2" in outcome["error"]
    assert session.scalar(select(StudyTaskRow).where(StudyTaskRow.subject == "Préfixe")) is None
    row = session.get(ProposalRow, uuid.UUID(proposal))
    assert row is not None and row.status == "pending"
    assert ctx.changed == [] and replan_request(session) is None


def test_idempotent_retry_after_a_failure_before_commit(
    sessions: sessionmaker[Session], database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"title": "Unique", "estimated_minutes": 60, "splittable": True}
    cycle = [response(tool_use("create_task", payload)), response(say("Tâche enregistrée."))]
    script = ScriptedModels(*cycle, *cycle)
    app = create_app(Settings(api_token=TOKEN, database_url=database_url))
    app.state.assistant = assistant(script)
    save = conversation_routes._remember_response
    attempts = 0

    def failed_once(*args: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("interruption avant le commit")
        save(*args)

    monkeypatch.setattr(conversation_routes, "_remember_response", failed_once)
    headers = {**AUTH, "Idempotency-Key": "once"}
    body = {"text": "Enregistre une heure pour Unique."}
    with TestClient(app, raise_server_exceptions=False) as api:
        assert api.post("/v1/conversation", headers=headers, json=body).status_code == 500
        with sessions() as session:
            assert session.scalar(select(StudyTaskRow)) is None
            assert replan_request(session) is None
        successful = api.post("/v1/conversation", headers=headers, json=body)
        replay = api.post("/v1/conversation", headers=headers, json=body)
        assert successful.status_code == replay.status_code == 200
        assert successful.json() == replay.json() and successful.json()["changed"]
    with sessions() as session:
        assert len(list(session.scalars(select(StudyTaskRow)))) == 1


def test_expired_idempotency_record_can_be_reused_before_the_worker_purge(
    sessions: sessionmaker[Session], database_url: str
) -> None:
    script = ScriptedModels(response(say("Premier.")), response(say("Second.")))
    app = create_app(Settings(api_token=TOKEN, database_url=database_url))
    app.state.assistant = assistant(script)
    headers = {**AUTH, "Idempotency-Key": "expired"}
    with TestClient(app) as api:
        assert (
            api.post("/v1/conversation", headers=headers, json={"text": "Premier"}).status_code
            == 200
        )
        with sessions.begin() as session:
            row = session.scalar(select(SettingRow).where(SettingRow.key.like("idem:%")))
            assert row is not None
            record = {
                **row.value["value"],
                "at": (datetime.now(UTC) - timedelta(days=3)).isoformat(),
            }
            row.value = {"value": record}
        second = api.post("/v1/conversation", headers=headers, json={"text": "Second"})
        assert second.status_code == 200 and second.json()["reply"] == "Second."


def test_model_chosen_in_home_assistant_is_used_without_restart(session: Session) -> None:
    from tempo.const import LLM_MODEL_SETTING
    from tempo.db.store import set_setting

    llm = ScriptedModels(response(say("Bonjour.")), response(say("Re.")))
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant")
    agent = assistant(llm)
    agent.reply(ctx, "salut", [])
    set_setting(session, LLM_MODEL_SETTING, "gemini-other-model")
    agent.reply(ctx, "salut", [])
    assert [r["model"] for r in llm.requests] == ["gemini-model-under-test", "gemini-other-model"]


def test_open_questions_are_asked_one_at_a_time_by_urgency(session: Session) -> None:
    """Questions persist in the database (not per conversation), the most urgent is given to
    the model and ends the answer, one at a time; reading one in Home Assistant does not answer
    it; once resolved, the next one follows in the same answer."""
    from datetime import timedelta

    from tempo.db.store import notify

    seed(session)
    for text_, key, days in (
        ("Où a lieu le TP ?", "question:tp", 1),
        ("Est-ce un examen ?", "question:exam", 3),
    ):
        notify(session, "question", "Q", text_, key, due_at=NOW + timedelta(days=days))
    for row in session.scalars(select(NotificationRow)):
        row.acknowledged_at = NOW  # Home Assistant delivered them
    session.flush()
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant", routing=FakeRouting())
    fresh = ScriptedModels(response(say("Bonjour !")))
    first = assistant(fresh).reply(ctx, "bonjour", [])  # a NEW conversation, no history
    sent = fresh.requests[0]["contents"][-1].parts[0].text
    assert "Où a lieu le TP ?" in sent and "1 autre(s)" in sent and "examen" not in sent
    assert first.text == "Bonjour !\nOù a lieu le TP ?"
    tp = next(n for n in session.scalars(select(NotificationRow)) if "TP" in n.message)
    tp.resolved_at = NOW  # answered: the planner no longer raises it
    session.flush()
    following = assistant(ScriptedModels(response(say("C'est noté.")))).reply(ctx, "salle B", [])
    assert following.text == "C'est noté.\nEst-ce un examen ?"


@pytest.mark.parametrize(("user", "thread"), [("a", "other"), ("b", "first")])
def test_only_owner_and_thread_can_resume_or_cancel(
    sessions: sessionmaker[Session], database_url: str, user: str, thread: str
) -> None:
    with sessions.begin() as session:
        seed(session)
    llm = ScriptedModels(
        response(
            tool_use(
                "clarify_request",
                {
                    "question": "Fractionnable en plusieurs séances ?",
                    "reason": "missing_information",
                },
            )
        )
    )
    app = create_app(Settings(api_token=TOKEN, database_url=database_url))
    app.state.assistant = assistant(llm)
    with TestClient(app) as api:
        owner = {"user_id": "a", "conversation_id": "first"}
        other = {"user_id": user, "conversation_id": thread}
        first = api.post(
            "/v1/conversation", headers=AUTH, json={**owner, "text": "rapport 3 h"}
        ).json()
        assert first["status"] == "missing_information"
        draft_id = first["draft_id"]
        public = api.post("/v1/operations/list_proposals", headers=AUTH, json={}).json()
        assert draft_id not in str(public)
        llm.script.append(response(tool_use("resume_request", {"draft_id": draft_id})))
        wrong = api.post("/v1/conversation", headers=AUTH, json={**other, "text": "oui"}).json()
        assert not wrong["changed"] and "introuvable" in wrong["reply"]
        sent = llm.requests[-1]["contents"]
        assert len(sent) == 1 and "rapport 3 h" not in sent[0].parts[0].text
        for command, scope in (("annule", other), ("confirme", owner), ("annule", owner)):
            outcome = api.post(
                "/v1/conversation",
                headers=AUTH,
                json={**scope, "text": f"{command} {draft_id[:8]}"},
            ).json()
            assert not outcome["changed"]
            assert outcome["status"] == (
                "rejected" if (command, scope) == ("annule", owner) else "interrupted"
            )
    with sessions() as session:
        draft = session.get(ProposalRow, uuid.UUID(draft_id))
        assert draft is not None and draft.status == "rejected"


def test_three_clarifications_preserve_and_edit_multiple_steps(session: Session) -> None:
    seed(session)
    ctx = OpContext(session, NOW, TZ, "assistant", FakeRouting(), user_id="student")
    work = {"title": "Projet", "estimated_minutes": 60, "splittable": False}
    event = {
        "title": "Rendez-vous",
        "scheduling": "fixed",
        "remote": True,
        "start": "2026-12-10T21:00",
        "duration_minutes": 30,
    }
    script = ScriptedModels(
        response(tool_use("create_task", work), tool_use("create_event", event)),
        response(
            tool_use("clarify_request", {"question": "Le titre du projet est-il définitif ?"})
        ),
    )
    first = assistant(script).reply(ctx, "Ajoute le projet et le rendez-vous.", [])
    draft_id = first.draft_id
    assert draft_id is not None
    row = session.get(ProposalRow, uuid.UUID(draft_id))
    assert row is not None
    responses = [
        {
            "edits": [{"step": 1, "arguments": {"title": "Projet physique"}}],
            "question": "Combien de temps pour le projet ?",
        },
        {
            "edits": [{"step": 1, "arguments": {"estimated_minutes": 120}}],
            "question": "Gardez-vous le rendez-vous à 21 h ?",
        },
        {},
    ]
    for extra, words in zip(responses, ("Projet physique", "2 heures", "oui"), strict=True):
        script = ScriptedModels(
            response(tool_use("resume_request", {"draft_id": draft_id, **extra}))
        )
        answer = assistant(script).reply(ctx, words, [])
        sent = script.requests[0]["contents"][-1].parts[0].text
        assert "Rendez-vous" in sent and draft_id in sent
        if "question" in extra:
            assert answer.draft_id == draft_id and answer.status == "clarification"
        else:
            assert answer.proposal_id == draft_id and answer.status == "pending_confirmation"
        assert ctx.changed == [] and replan_request(session) is None
    assert [step["arguments"] for step in row.details["plan_changes"]] == [
        {**work, "title": "Projet physique", "estimated_minutes": 120},
        event,
    ]


def test_independent_request_and_idempotent_clarification(
    sessions: sessionmaker[Session], database_url: str
) -> None:
    with sessions.begin() as session:
        seed(session)
    work = {"title": "Projet en attente", "estimated_minutes": 60, "splittable": True}
    other = {"title": "Projet indépendant", "estimated_minutes": 45, "splittable": True}
    llm = ScriptedModels(
        response(
            tool_use(
                "clarify_request",
                {
                    "changes": [{"name": "create_task", "arguments": work}],
                    "question": "Quelle échéance ?",
                },
            )
        ),
        response(
            tool_use(
                "preview_plan_changes",
                {
                    "changes": [{"name": "create_task", "arguments": other}],
                },
            )
        ),
    )
    app = create_app(Settings(api_token=TOKEN, database_url=database_url))
    app.state.assistant = assistant(llm)
    app.state.routing = FakeRouting()
    app.state.clock = lambda: NOW
    with TestClient(app) as api:
        key = {**AUTH, "Idempotency-Key": "clarification-retry"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [
                pool.submit(
                    api.post, "/v1/conversation", headers=key, json={"text": "Ajoute le projet"}
                )
                for _ in range(2)
            ]
            first, retry = [request.result(timeout=20).json() for request in requests]
        assert first == retry
        second = api.post(
            "/v1/conversation", headers=AUTH, json={"text": "Prépare un autre projet indépendant"}
        ).json()
    assert len(llm.requests) == 2
    with sessions() as session:
        draft = session.get(ProposalRow, uuid.UUID(first["draft_id"]))
        independent = session.get(ProposalRow, uuid.UUID(second["proposal_id"]))
        assert draft is not None and independent is not None and draft.id != independent.id
        assert draft.details["changes"][0]["arguments"] == work
        assert independent.details["plan_changes"] == [{"name": "create_task", "arguments": other}]


def test_expired_draft_cannot_be_resumed(session: Session) -> None:
    seed(session)
    ctx = OpContext(session, NOW, TZ, "assistant", user_id="student")
    first = assistant(
        ScriptedModels(response(tool_use("clarify_request", {"question": "Quelle date ?"})))
    ).reply(ctx, "Ajoute un rendez-vous", [])
    assert first.draft_id is not None
    later = ScriptedModels(response(tool_use("resume_request", {"draft_id": first.draft_id})))
    result = assistant(later).reply(replace(ctx, now=NOW + timedelta(hours=24)), "demain", [])
    assert result.status == "interrupted" and "expiré" in result.text
    assert "Quelle date" not in later.requests[0]["contents"][-1].parts[0].text


def test_text_question_does_not_suspend_prepared_operation(session: Session) -> None:
    seed(session)
    ctx = OpContext(session, NOW, TZ, "assistant", routing=FakeRouting(), user_id="student")
    script = ScriptedModels(
        response(
            tool_use(
                "create_task",
                {
                    "title": "Projet",
                    "estimated_minutes": 60,
                    "splittable": True,
                },
            )
        ),
        response(say("Projet ajouté. Autre chose ?")),
    )
    answer = assistant(script).reply(ctx, "Ajoute le projet", [])
    assert answer.status == "completed" and answer.draft_id is None
    assert ctx.changed == ["create_task", "plan_now"]


def test_draft_replays_dependent_changes_with_actual_ids(session: Session) -> None:
    seed(session)
    ctx = OpContext(session, NOW, TZ, "assistant", FakeRouting(), user_id="student")
    script = ScriptedModels(
        response(
            tool_use(
                "clarify_request",
                {
                    "changes": [
                        {
                            "name": "create_task",
                            "ref": "task",
                            "arguments": {
                                "title": "Projet",
                                "estimated_minutes": 60,
                                "splittable": True,
                            },
                        }
                    ],
                    "question": "Quelle durée finale ?",
                },
            )
        ),
    )
    first = assistant(script).reply(ctx, "Prépare le projet", [])
    assert first.draft_id is not None
    resumed = assistant(
        ScriptedModels(
            response(
                tool_use(
                    "resume_request",
                    {
                        "draft_id": first.draft_id,
                        "changes": [
                            {
                                "name": "update_task",
                                "arguments": {
                                    "task_id": {"$ref": "task.task.id"},
                                    "estimated_minutes": 120,
                                },
                            }
                        ],
                    },
                )
            )
        )
    ).reply(ctx, "2 heures", [])
    assert resumed.proposal_id == first.draft_id, resumed.text
    approval = run_operation(
        ctx,
        "decide_proposal",
        {
            "proposal_id": first.draft_id,
            "approve": True,
        },
    )
    assert approval["ok"], approval
    task = session.scalars(select(StudyTaskRow).where(StudyTaskRow.subject == "Projet")).one()
    assert task.effort_minutes == 120
