"""Conversation and operation routes (bearer token required, mounted in ``api.app``).

- ``POST /v1/conversation``: the natural-language interface used by Home Assistant (text or
  voice). The LLM assistant runs catalogue operations; without LLM (no key, outage, refusal)
  the deterministic fallback (``assistant.fallback``) answers through the same operations.
- ``GET /v1/operations`` / ``POST /v1/operations/{name}``: the typed operation catalogue for
  authenticated clients (same validation and rules as the assistant). An ``Idempotency-Key``
  header makes a mutation safe to retry.
"""

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from tempo.api.location import SessionDep
from tempo.assistant import fallback
from tempo.assistant.agent import Assistant, LLMUnavailableError
from tempo.assistant.catalog import CATALOG, OpContext, run_operation
from tempo.db.models import ConversationTurnRow
from tempo.planning.service import get_setting, set_setting
from tempo.transport.routing import RoutingProvider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1")
HISTORY_TURNS = 12
HISTORY_WINDOW = timedelta(hours=6)


def _context(request: Request, session: Any, actor: str) -> OpContext:
    routing: RoutingProvider | None = getattr(request.app.state, "routing", None)
    return OpContext(
        session=session,
        now=datetime.now(UTC),
        tz=request.app.state.timezone,
        actor=actor,
        routing=routing,
    )


@router.get("/operations")
def list_operations() -> list[dict[str, Any]]:
    return [
        {
            "name": op.name,
            "description": op.description,
            "mutates": op.mutates,
            "input_schema": op.model.model_json_schema(),
        }
        for op in sorted(CATALOG.values(), key=lambda o: o.name)
    ]


@router.post("/operations/{name}")
def call_operation(
    name: str,
    body: dict[str, Any],
    request: Request,
    session: SessionDep,
    idempotency_key: Annotated[str | None, Header(max_length=150)] = None,
) -> dict[str, Any]:
    if name not in CATALOG:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown operation")
    key = None
    if idempotency_key and CATALOG[name].mutates:
        digest = hashlib.sha256(f"{name}:{idempotency_key}".encode()).hexdigest()[:40]
        key = f"idem:{digest}"
        previous = get_setting(session, key)
        if previous is not None:
            return dict(previous["response"])
    outcome = run_operation(_context(request, session, "api"), name, body)
    if not outcome["ok"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, outcome["error"])
    if key is not None:
        stored = {"at": datetime.now(UTC).isoformat(), "response": outcome}
        set_setting(session, key, _jsonable(stored))
    return outcome


def _jsonable(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, default=str))


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    conversation_id: str = Field(default="default", min_length=1, max_length=100)


def _history(session: Any, conversation_id: str, now: datetime) -> list[dict[str, str]]:
    rows = list(
        session.scalars(
            select(ConversationTurnRow)
            .where(
                ConversationTurnRow.conversation_id == conversation_id,
                ConversationTurnRow.created_at >= now - HISTORY_WINDOW,
            )
            .order_by(ConversationTurnRow.created_at.desc())
            .limit(HISTORY_TURNS)
        )
    )
    turns = [{"role": r.role, "content": r.content} for r in reversed(rows)]
    while turns and turns[0]["role"] != "user":
        turns.pop(0)  # the API requires the first message to be the user's
    return turns


def _remember(session: Any, conversation_id: str, role: str, content: str) -> None:
    session.add(
        ConversationTurnRow(
            conversation_id=conversation_id,
            role=role,
            content=content[:8000],
            created_at=datetime.now(UTC),
        )
    )
    session.flush()


@router.post("/conversation")
def conversation(body: ConversationIn, request: Request, session: SessionDep) -> dict[str, Any]:
    assistant: Assistant | None = getattr(request.app.state, "assistant", None)
    if assistant is not None:
        ctx = _context(request, session, "assistant")
        history = _history(session, body.conversation_id, ctx.now)
        try:
            reply = assistant.reply(ctx, body.text, history)
        except LLMUnavailableError as exc:
            logger.warning("assistant unavailable (%s): deterministic fallback", exc)
        else:
            _remember(session, body.conversation_id, "user", body.text)
            _remember(session, body.conversation_id, "assistant", reply.text)
            return {
                "reply": reply.text,
                "used_llm": True,
                "operations": reply.operations,
                "changed": bool(ctx.changed),
            }
    ctx = _context(request, session, "assistant")
    result = fallback.reply(ctx, body.text[:2000])
    _remember(session, body.conversation_id, "user", body.text)
    _remember(session, body.conversation_id, "assistant", result["reply"])
    return {
        "reply": result["reply"],
        "used_llm": False,
        "operations": ctx.changed,
        "changed": result["changed"],
        "fallback": True,
    }
