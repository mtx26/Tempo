"""``POST /v1/interpret``: natural-language requests from Home Assistant (voice or text).

Deterministic parser first; the optional LLM is consulted only for text the parser does not
understand, and its output is validated like any request. Without an API key Tempo answers
"not understood" with examples of supported phrasings: essential functions never depend on AI.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from tempo.ai.actions import (
    Action,
    block_to_complete,
    parse_action,
    pending_proposals,
    subject_of,
)
from tempo.ai.apply import apply_result, from_extraction, known_places, result_from_json
from tempo.ai.llm import Interpreter
from tempo.ai.questions import answer, parse_question
from tempo.ai.text_parser import parse_text
from tempo.api.ha import SessionDep
from tempo.api.planning import PROPOSAL_HANDLERS, _decide, block_done
from tempo.db.models import ActivityRow, ProposalRow
from tempo.planning.service import audit, notify, set_setting

router = APIRouter(prefix="/v1")
EXAMPLES = (
    "Exemples : « ajoute 5h de maths pour vendredi », « ajoute 90 min d'analyse avant le "
    "18/12 strict », « mercredi 16/12 18h30 : fractionné 60 min @club - 6 x 800 m », "
    "« course samedi 19/12 10:00 : 10 km 60 min @namur », « replanifie », « qu'est-ce que "
    "j'ai demain ? », « prochain départ ? », « combien d'heures d'étude il me reste ? »."
)


class InterpretIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)


@router.post("/interpret")
def interpret(body: InterpretIn, request: Request, session: SessionDep) -> dict[str, Any]:
    tz: ZoneInfo = request.app.state.timezone
    interpreter: Interpreter | None = getattr(request.app.state, "interpreter", None)
    now = datetime.now(UTC)
    today = now.astimezone(tz).date()
    action = parse_action(body.text)
    if action is not None:
        return _act(session, action, now, tz)
    question = parse_question(body.text, today)
    if question is not None:
        return {
            "understood": True,
            "action": "answer",
            "summary": answer(session, question, now, tz),
            "applied": False,
            "used_llm": False,
        }
    result = parse_text(body.text, today)
    rejected: list[str] = []
    used_llm = False
    if not result.understood and interpreter is not None:
        extraction = interpreter.extract(body.text, today, sorted(known_places(session)))
        if extraction is not None:
            result, rejected = from_extraction(extraction)
            used_llm = True
    applied = apply_result(
        session, result, tz=tz, today=today, source="assistant_llm" if used_llm else "assistant"
    )
    rejected += applied.rejected
    changed = bool(applied.created) or result.replan
    if changed:
        set_setting(session, "replan_requested", datetime.now(UTC).isoformat())
    audit(
        session,
        "home_assistant",
        "interpret",
        "request",
        "-",
        llm=used_llm,
        created=len(applied.created),
        rejected=len(rejected),
    )
    parts = []
    if applied.created:
        parts.append("Ajouté : " + " ; ".join(applied.created) + ".")
    if result.replan:
        parts.append("Replanification demandée.")
    if rejected:
        parts.append("Refusé : " + " ; ".join(rejected) + ".")
    if not changed and not rejected:
        parts.append("Je n'ai pas compris la demande. " + EXAMPLES)
    return {
        "understood": changed or bool(rejected),
        "action": "add" if applied.created else ("replan" if result.replan else None),
        "summary": " ".join(parts),
        "applied": changed,
        "used_llm": used_llm,
    }


def _apply_training_import(session: Session, row: ProposalRow) -> None:
    """Approved AI-interpreted e-mail content: insert it (still validated, idempotent)."""
    details = row.details
    tz = ZoneInfo(details["timezone"])
    applied = apply_result(
        session,
        result_from_json(details["result"]),
        tz=tz,
        today=datetime.now(UTC).astimezone(tz).date(),  # re-validated at approval time
        source="email_llm",
        source_ref=details["source_ref"],
    )
    if applied.rejected:
        notify(
            session,
            "training_rejected",
            "Import refusé en partie",
            " ; ".join(applied.rejected),
            f"approval_rejected:{row.id}",
        )


PROPOSAL_HANDLERS["training_import"] = _apply_training_import


DATE_FIELDS = frozenset({"preferred_day"})
DATETIME_FIELDS = frozenset({"earliest_start_at", "latest_end_at"})


def _apply_coach_change(session: Session, row: ProposalRow) -> None:
    """Approved coach change on a protected session: take the coach's version (validated keys
    only); the session then follows the coach again (``user_modified`` cleared)."""
    activity = session.get(ActivityRow, uuid.UUID(row.details["activity_id"]))
    if activity is None or activity.status == "done":
        return
    allowed = {
        "title",
        "notes",
        "duration_minutes",
        "earliest_start_at",
        "latest_end_at",
        "preferred_day",
        "status",
    }
    for key, value in row.details["values"].items():
        if key not in allowed:
            continue
        if key in DATE_FIELDS and value is not None:
            value = date.fromisoformat(value)
        elif key in DATETIME_FIELDS and value is not None:
            value = datetime.fromisoformat(value)
        setattr(activity, key, value)
    activity.user_modified = False
    activity.locked = False


PROPOSAL_HANDLERS["coach_plan_change"] = _apply_coach_change


def _reply(summary: str, applied: bool, action: str) -> dict[str, Any]:
    return {
        "understood": True,
        "action": action,
        "summary": summary,
        "applied": applied,
        "used_llm": False,
    }


def _act(session: Session, action: Action, now: datetime, tz: ZoneInfo) -> dict[str, Any]:
    if action.kind in ("approve", "reject"):
        pending = pending_proposals(session)
        if not pending:
            return _reply("Aucune proposition en attente.", False, action.kind)
        if len(pending) > 1:
            titles = " ; ".join(p.title for p in pending[:5])
            return _reply(
                f"{len(pending)} propositions en attente ({titles}). Choisissez laquelle dans "
                "Home Assistant : je ne décide pas à votre place.",
                False,
                action.kind,
            )
        decided = _decide(session, pending[0].id, action.kind == "approve")
        verb = "approuvée" if action.kind == "approve" else "refusée"
        return _reply(f"Proposition {verb} : {decided['title']}.", True, action.kind)
    block = block_to_complete(session, now, tz)
    if block is None:
        return _reply(
            "Je ne trouve pas de session d'étude en cours ou terminée aujourd'hui.", False, "done"
        )
    block_done(block.id, session)
    start = block.start_at.astimezone(tz).strftime("%H:%M")
    return _reply(
        f"Session « {subject_of(session, block)} » de {start} marquée comme faite.", True, "done"
    )
