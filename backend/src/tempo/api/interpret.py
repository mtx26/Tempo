"""``POST /v1/interpret``: natural-language requests from Home Assistant (voice or text).

Deterministic parser first; the optional LLM is consulted only for text the parser does not
understand, and its output is validated like any request. Without an API key Tempo answers
"not understood" with examples of supported phrasings: essential functions never depend on AI.
"""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from tempo.ai.apply import apply_result, from_extraction, known_places, result_from_json
from tempo.ai.llm import Interpreter
from tempo.ai.questions import answer, parse_question
from tempo.ai.text_parser import parse_text
from tempo.api.ha import SessionDep
from tempo.api.planning import PROPOSAL_HANDLERS
from tempo.db.models import ProposalRow
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
