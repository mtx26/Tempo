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
import json
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tempo.api.location import SessionDep
from tempo.assistant.agent import Assistant, LLMUnavailableError
from tempo.assistant.catalog import CATALOG, OpContext, run_operation
from tempo.const import (
    CONVERSATION_DRAFT_KIND,
    HISTORY_TURNS,
    HISTORY_WINDOW,
    IDEMPOTENCY_RETENTION,
)
from tempo.db.models import ConversationTurnRow
from tempo.db.store import get_setting, set_setting
from tempo.transport.routing import RoutingProvider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1")
IdempotencyKey = Annotated[str | None, Header(max_length=150)]
PROPOSAL_DECISION = re.compile(
    r"(confirme|accepte|refuse|annule)\s+(?:(?:la proposition|le brouillon)\s+)?"
    r"([0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?)"
    r"[.!]?",
    re.IGNORECASE,
)


def _context(request: Request, session: Any, actor: str) -> OpContext:
    routing: RoutingProvider | None = getattr(request.app.state, "routing", None)
    return OpContext(
        session=session,
        now=request.app.state.clock(),
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
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    if name not in CATALOG:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown operation")
    key = fingerprint = None
    if idempotency_key and CATALOG[name].mutates:
        key, fingerprint, previous = _idempotency(session, name, idempotency_key, body)
        if previous is not None:
            return previous
    outcome = run_operation(_context(request, session, "api"), name, body)
    if not outcome["ok"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, outcome["error"])
    if key is not None:
        _remember_response(session, key, fingerprint, outcome)
    return outcome


def _idempotency(
    session: Session, operation: str, identifier: str, body: dict[str, Any]
) -> tuple[str, str, dict[str, Any] | None]:
    digest = hashlib.sha256(f"{operation}:{identifier}".encode()).hexdigest()[:40]
    key = f"idem:{digest}"
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    # A retry waits for the response and writes to commit together.
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
    previous = get_setting(session, key)
    if previous is None or (
        datetime.now(UTC) - datetime.fromisoformat(previous["at"]) >= IDEMPOTENCY_RETENTION
    ):
        return key, fingerprint, None
    if previous.get("fingerprint") != fingerprint:
        raise HTTPException(status.HTTP_409_CONFLICT, "idempotency key already used")
    return key, fingerprint, dict(previous["response"])


def _remember_response(
    session: Session, key: str, fingerprint: str | None, response: dict[str, Any]
) -> None:
    stored = {"at": datetime.now(UTC).isoformat(), "fingerprint": fingerprint, "response": response}
    set_setting(session, key, json.loads(json.dumps(stored, default=str)))


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    conversation_id: str = Field(default="default", min_length=1, max_length=100)
    user_id: str = Field(default="tempo", min_length=1, max_length=100)


def _history(session: Session, scope: str, now: datetime) -> list[dict[str, str]]:
    rows = list(
        session.scalars(
            select(ConversationTurnRow)
            .where(
                ConversationTurnRow.conversation_id == scope,
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


def _remember(session: Session, scope: str, role: str, content: str) -> None:
    session.add(
        ConversationTurnRow(
            conversation_id=scope,
            role=role,
            content=content[:8000],
            created_at=datetime.now(UTC),
        )
    )
    session.flush()


@router.post("/conversation")
def conversation(
    body: ConversationIn,
    request: Request,
    session: SessionDep,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    key = fingerprint = None
    if idempotency_key:
        payload = body.model_dump(mode="json")
        if body.user_id == "tempo":
            payload.pop("user_id")
        key, fingerprint, previous = _idempotency(session, "conversation", idempotency_key, payload)
        if previous is not None:
            return previous
    ctx = _context(request, session, "assistant")
    ctx.user_id = body.user_id
    ctx.conversation_id = body.conversation_id
    # A stable, unambiguous key for both history and serialization of turns in the same thread.
    scope = (
        "scope:"
        + hashlib.sha256(json.dumps([body.user_id, body.conversation_id]).encode()).hexdigest()
    )
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('conversation'), hashtext(:scope))"),
        {"scope": scope},
    )
    decision = PROPOSAL_DECISION.fullmatch(body.text.strip())
    if decision:
        response = _decide(ctx, decision[2].lower(), decision[1].lower() in {"confirme", "accepte"})
    else:
        assistant: Assistant | None = getattr(request.app.state, "assistant", None)
        if assistant is None:
            return _unavailable("assistant Gemini non configuré (compte de service Vertex AI)")
        history = _history(session, scope, ctx.now)
        try:
            reply = assistant.reply(ctx, body.text, history)
        except LLMUnavailableError as exc:
            logger.warning("assistant unavailable: %s", exc)
            return _unavailable(str(exc))
        response = {
            "reply": reply.text,
            "used_llm": True,
            "operations": reply.operations,
            "changed": bool(ctx.changed),
            "status": reply.status,
        }
        if reply.proposal_id is not None:
            response["proposal_id"] = reply.proposal_id
        if reply.draft_id is not None:
            response["draft_id"] = reply.draft_id
    _remember(session, scope, "user", body.text)
    _remember(session, scope, "assistant", response["reply"])
    if key is not None:
        _remember_response(session, key, fingerprint, response)
    return response


def _decide(ctx: OpContext, code: str, approve: bool) -> dict[str, Any]:
    outcome = run_operation(ctx, "decide_proposal", {"proposal_id": code, "approve": approve})
    result = outcome.get("result", {})
    proposal = result.get("proposal", {})
    state = proposal.get("status")
    is_draft = proposal.get("kind") == CONVERSATION_DRAFT_KIND
    return {
        "reply": {
            "approved": "Proposition approuvée. Les changements confirmés ont été appliqués.",
            "rejected": "Demande abandonnée." if is_draft else "Proposition refusée.",
            "obsolete": "La proposition a expiré ou le planning a changé. "
            "Demandez un nouvel aperçu.",
        }.get(state, outcome.get("error", "Proposition indisponible.")),
        "used_llm": False,
        "operations": [
            {"name": "decide_proposal", "ok": outcome["ok"], "error": outcome.get("error")}
        ],
        "changed": not is_draft and state in {"approved", "rejected"},
        "status": "completed" if state == "approved" else state or "interrupted",
    }


def _unavailable(reason: str) -> dict[str, Any]:
    """Gemini could not answer: nothing was changed, the user is told so."""
    return {
        "reply": f"Je ne peux pas traiter la demande pour l'instant : {reason}. Rien n'a été "
        "modifié ; réessayez dans un moment.",
        "used_llm": False,
        "operations": [],
        "changed": False,
        "status": "interrupted",
        "error": "llm_unavailable",
    }
