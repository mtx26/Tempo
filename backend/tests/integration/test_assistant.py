"""Conversational assistant on a REAL PostgreSQL with a scripted LLM client.

The scripted client stands in for Claude (no network, no cost): it proves the harness runs
the operations the model asks for, returns their real results, keeps going until the model
answers, and falls back to the deterministic parser when the LLM is unavailable. A real
model is exercised only by ``test_assistant_live.py`` (needs an API key)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tempo.api.app import create_app
from tempo.assistant.agent import ClaudeAssistant, LLMUnavailableError
from tempo.assistant.catalog import OpContext
from tempo.config import Settings
from tempo.db.models import ActivityRow, Base, ConversationTurnRow, StudyTaskRow
from tests.integration.test_planning_service import NOW, TZ, seed
from tests.test_engine import FakeRouting

pytestmark = pytest.mark.integration
TOKEN = "Hq8Wm2Zr5Tx9Lb3Nc7Vd1Kf4Gs6Pj0YeUa"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def tool_use(name: str, arguments: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_use", id=f"tu_{uuid.uuid4().hex[:8]}", name=name, input=arguments
    )


def say(words: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=words)


def response(stop: str, *blocks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop, content=list(blocks))


class ScriptedMessages:
    """Returns scripted responses in order and records every request."""

    def __init__(self, *script: SimpleNamespace | Exception) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def tool_results(request: dict[str, Any]) -> list[dict[str, Any]]:
    last = request["messages"][-1]
    assert last["role"] == "user"
    return list(last["content"])


def test_compound_request_runs_operations_and_reports_real_results(session: Session) -> None:
    seed(session)
    llm = ScriptedMessages(
        response("tool_use", say("Je regarde."), tool_use("get_context", {})),
        response(
            "tool_use",
            tool_use(
                "create_task",
                {"title": "Physique", "estimated_minutes": 900, "target_date": "2026-12-11"},
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
        response("tool_use", tool_use("plan_now", {})),
        response("end_turn", say("C'est noté : 15 h de physique et le dentiste jeudi.")),
    )
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant", routing=FakeRouting())
    reply = ClaudeAssistant(messages=llm).reply(
        ctx, "15 h de physique avant vendredi et dentiste jeudi 21h", []
    )
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
    assert len(results) == 2 and not any(r["is_error"] for r in results)
    plan = tool_results(llm.requests[3])[0]["content"]
    assert '"published": true' in plan
    assert session.scalars(select(StudyTaskRow).where(StudyTaskRow.subject == "Physique")).one()
    # the request carries the stable cached system prompt, tools and today's date
    first = llm.requests[0]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert any(t["name"] == "create_event" for t in first["tools"])
    assert "2026" in first["messages"][-1]["content"]
    assert ctx.changed == ["create_task", "create_event", "plan_now"]


def test_failed_operation_is_reported_to_the_model_and_rolled_back(session: Session) -> None:
    seed(session)
    llm = ScriptedMessages(
        response(
            "tool_use",
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
        response("end_turn", say("Combien de temps dure la sortie ?")),
    )
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant")
    reply = ClaudeAssistant(messages=llm).reply(ctx, "ajoute une sortie vendredi soir", [])
    assert reply.text.endswith("?")
    result = tool_results(llm.requests[1])[0]
    assert result["is_error"] and "durée inconnue" in result["content"]
    assert session.scalars(select(ActivityRow).where(ActivityRow.title == "Sortie")).first() is None
    assert ctx.changed == []


def test_unavailable_llm_raises_for_fallback(session: Session) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    llm = ScriptedMessages(anthropic.APIConnectionError(request=request))
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant")
    with pytest.raises(LLMUnavailableError):
        ClaudeAssistant(messages=llm).reply(ctx, "bonjour", [])
    refused = ScriptedMessages(response("refusal"))
    with pytest.raises(LLMUnavailableError):
        ClaudeAssistant(messages=refused).reply(ctx, "bonjour", [])


def test_runaway_loop_is_bounded(session: Session) -> None:
    seed(session)
    llm = ScriptedMessages(*[response("tool_use", tool_use("get_summary", {})) for _ in range(20)])
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant")
    reply = ClaudeAssistant(messages=llm).reply(ctx, "boucle", [])
    assert "trop longue" in reply.text and len(llm.requests) == 16


@pytest.fixture
def sessions(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


def test_conversation_route_keeps_context_and_falls_back(
    sessions: sessionmaker[Session], database_url: str
) -> None:
    with sessions() as s:
        seed(s)
        s.commit()
    llm = ScriptedMessages(
        response("tool_use", tool_use("list_tasks", {})),
        response("end_turn", say("Vous avez une tâche : maths.")),
        response("end_turn", say("Elle compte 300 minutes.")),
        anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
    )
    app = create_app(Settings(api_token=TOKEN, database_url=database_url))
    with TestClient(app) as api:
        app.state.assistant = ClaudeAssistant(messages=llm)
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
        history = llm.requests[2]["messages"]
        assert history[0] == {"role": "user", "content": "quelles tâches ?"}
        assert history[1]["role"] == "assistant"
        # LLM outage: the deterministic parser still answers, nothing is invented
        third = api.post(
            "/v1/conversation",
            headers=AUTH,
            json={"text": "combien d'heures d'étude il me reste ?"},
        )
        assert third.status_code == 200
        assert third.json()["used_llm"] is False and third.json()["fallback"] is True
        assert "300" in third.json()["reply"] or "5" in third.json()["reply"]
        assert api.post("/v1/conversation", json={"text": "x"}).status_code == 401
    with sessions() as s:
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
        body = {"title": "Rapport", "kind": "personal", "estimated_minutes": 60}
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
