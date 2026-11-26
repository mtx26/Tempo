"""Conversation and operation routes (bearer token required, mounted in ``api.app``).

- ``POST /v1/conversation``: the natural-language interface used by Home Assistant (text or
  voice). The Gemini assistant runs catalogue operations; when Gemini is unavailable (not
  configured, outage, refusal) the error is reported to the user as is: there is no second
  conversational path. Planning and background jobs never depend on it.
- ``GET /v1/operations`` / ``POST /v1/operations/{name}``: the typed operation catalogue for
  authenticated clients (same validation and rules as the assistant). An ``Idempotency-Key``
  header makes a mutation safe to retry.
"""

import hashlib
import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from tempo.api.location import SessionDep
from tempo.assistant.agent import Assistant, LLMUnavailableError
from tempo.assistant.catalog import CATALOG, OpContext, run_operation
from tempo.const import HISTORY_TURNS, HISTORY_WINDOW
from tempo.db.models import ConversationTurnRow
from tempo.db.store import get_setting, set_setting
from tempo.transport.routing import RoutingProvider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1")


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
        # Concurrent retries with the same key wait here: the second one replays the first's
        # stored response instead of running the operation twice (lock held until commit).
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
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
    if assistant is None:
        return _unavailable("assistant Gemini non configuré (compte de service Vertex AI)")
    ctx = _context(request, session, "assistant")
    history = _history(session, body.conversation_id, ctx.now)
    try:
        reply = assistant.reply(ctx, body.text, history)
    except LLMUnavailableError as exc:
        logger.warning("assistant unavailable: %s", exc)
        return _unavailable(str(exc))
    _remember(session, body.conversation_id, "user", body.text)
    _remember(session, body.conversation_id, "assistant", reply.text)
    return {
        "reply": reply.text,
        "used_llm": True,
        "operations": reply.operations,
        "changed": bool(ctx.changed),
    }


def _unavailable(reason: str) -> dict[str, Any]:
    """Gemini could not answer: nothing was changed, the user is told so."""
    return {
        "reply": f"Je ne peux pas traiter la demande pour l'instant : {reason}. Rien n'a été "
        "modifié ; réessayez dans un moment.",
        "used_llm": False,
        "operations": [],
        "changed": False,
        "error": "llm_unavailable",
    }
