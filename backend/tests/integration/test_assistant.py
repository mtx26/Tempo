"""Conversational assistant on a REAL PostgreSQL with a scripted Gemini client.

The scripted client stands in for Gemini on Vertex AI (no network, no cost): it proves the
harness runs the operations the model asks for, returns their real results, keeps going until
the model answers, and falls back to the deterministic parser when the LLM is unavailable."""

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import types
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tempo.api.app import create_app
from tempo.assistant.agent import SYSTEM, GeminiChat, LLMUnavailableError, ToolLoopAssistant
from tempo.assistant.catalog import OpContext
from tempo.config import Settings
from tempo.db.models import (
    ActivityRow,
    Base,
    ConversationTurnRow,
    NotificationRow,
    StudyTaskRow,
)
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


def test_compound_request_runs_operations_and_reports_real_results(session: Session) -> None:
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
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant", routing=FakeRouting())
    reply = assistant(llm).reply(ctx, "15 h de physique avant vendredi et dentiste jeudi 21h", [])
    assert reply.text.startswith("C'est noté")
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
    assert ctx.changed == ["create_task", "create_event", "plan_now"]


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


def test_llm_outage_after_a_change_never_replays_the_request(session: Session) -> None:
    """The deterministic path must not run the same text again over applied changes."""
    seed(session)
    llm = ScriptedModels(
        response(
            tool_use(
                "create_task", {"title": "Chimie", "estimated_minutes": 60, "splittable": True}
            )
        ),
        outage(),
    )
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant")
    reply = assistant(llm).reply(ctx, "ajoute 1 h de chimie", [])
    assert "indisponible" in reply.text and "create_task" in reply.text
    # Never replayed; what was applied is still planned (deterministic re-plan request).
    assert ctx.changed == ["create_task", "plan_now"]


def test_time_budget_ends_with_the_real_outcome(session: Session) -> None:
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
    assert "manque de temps" in reply.text and "create_task" in reply.text
    assert len(llm.requests) == 1
    assert llm.requests[0]["config"].http_options.timeout == 60_000


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


def test_history_is_shared_by_every_assist_conversation(
    sessions: sessionmaker[Session], database_url: str
) -> None:
    """A clarification asked in one Assist conversation is still known in the next one."""
    with sessions() as s:
        seed(s)
        s.commit()
    llm = ScriptedModels(
        response(say("Fractionnable en plusieurs séances ?")), response(say("Noté."))
    )
    app = create_app(Settings(api_token=TOKEN, database_url=database_url))
    with TestClient(app) as api:
        app.state.assistant = assistant(llm)
        for words, conversation in (("rapport 3 h", "a"), ("oui", "b")):
            api.post(
                "/v1/conversation",
                headers=AUTH,
                json={"text": words, "conversation_id": conversation},
            )
    second = llm.requests[1]["contents"]
    assert second[0].parts[0].text == "rapport 3 h"  # the earlier conversation is in context
