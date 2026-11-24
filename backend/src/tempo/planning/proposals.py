"""Decisions on proposals (ARCHITECTURE.md §9): changes to protected commitments applied only
after the user's natural-language approval, re-validated against their input revision."""

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from tempo.ai.apply import apply_result, result_from_json
from tempo.db.models import ActivityRow, ProposalRow, StudyBlockRow
from tempo.domain.ops import DomainError
from tempo.planning.service import audit, notify, proposal_revision, set_setting


def proposal_out(row: ProposalRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "title": row.title,
        "details": row.details,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }


def _apply_unlock(session: Session, row: ProposalRow) -> None:
    block = session.get(StudyBlockRow, uuid.UUID(row.details["block_id"]))
    if block is not None and block.locked:
        session.delete(block)


def _apply_unlock_activity(session: Session, row: ProposalRow) -> None:
    activity = session.get(ActivityRow, uuid.UUID(row.details["activity_id"]))
    if activity is not None and activity.locked:
        activity.locked = False  # flexible again: the next re-plan moves it around the class


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


COACH_FIELDS = frozenset(
    {
        "title",
        "notes",
        "duration_minutes",
        "earliest_start_at",
        "latest_end_at",
        "preferred_day",
        "status",
    }
)


def _apply_coach_change(session: Session, row: ProposalRow) -> None:
    """Approved coach change on a protected session: take the coach's version (validated keys
    only); the session then follows the coach again (``user_modified`` cleared)."""
    activity = session.get(ActivityRow, uuid.UUID(row.details["activity_id"]))
    if activity is None or activity.status == "done":
        return
    for key, value in row.details["values"].items():
        if key not in COACH_FIELDS:
            continue
        if key == "preferred_day" and value is not None:
            value = date.fromisoformat(value)
        elif key in ("earliest_start_at", "latest_end_at") and value is not None:
            value = datetime.fromisoformat(value)
        setattr(activity, key, value)
    activity.user_modified = False
    activity.locked = False


PROPOSAL_HANDLERS: dict[str, Callable[[Session, ProposalRow], None]] = {
    "unlock_block": _apply_unlock,
    "unlock_activity": _apply_unlock_activity,
    "training_import": _apply_training_import,
    "coach_plan_change": _apply_coach_change,
}
"""What approving a proposal does, per kind."""


def decide(
    session: Session, proposal_id: uuid.UUID, approve: bool, *, actor: str, now: datetime
) -> dict[str, Any]:
    """Approve or reject a pending proposal. Approval re-checks the input revision: changed
    inputs make the proposal ``obsolete`` and nothing is applied (a re-plan is requested)."""
    row = session.get(ProposalRow, proposal_id, with_for_update=True)
    if row is None:
        raise DomainError("proposition introuvable")
    if row.status != "pending":
        raise DomainError(f"proposition déjà {row.status}")
    if (
        approve
        and row.input_revision is not None
        and proposal_revision(session, row.details) != row.input_revision
    ):
        row.status, row.decided_at = "obsolete", now
        set_setting(session, "replan_requested", now.isoformat())
        audit(session, actor, "proposal_obsolete", "proposal", str(row.id), kind=row.kind)
        return proposal_out(row)
    row.status, row.decided_at = ("approved" if approve else "rejected"), now
    if approve:
        handler = PROPOSAL_HANDLERS.get(row.kind)
        if handler is not None:
            handler(session, row)
        set_setting(session, "replan_requested", now.isoformat())
    audit(session, actor, "proposal_" + row.status, "proposal", str(row.id), kind=row.kind)
    return proposal_out(row)
