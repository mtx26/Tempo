"""Deterministic conversational actions (no LLM): approve/reject, "session done".

- "approuve" / "valide" / "accepte" and "refuse" / "rejette": applied only when exactly ONE
  proposal is pending; otherwise the user is asked to choose in Home Assistant (never guessed).
- "c'est fait" / "session terminée" / "j'ai fini ma session": marks done the study block under
  way, or else the latest block of the current plan that ended today.
"""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tempo.db.models import ProposalRow, StudyBlockRow, StudyTaskRow
from tempo.planning.service import active_version
from tempo.text import plain


def _plain(text: str) -> str:
    return plain(text).strip()


@dataclass(frozen=True, slots=True)
class Action:
    kind: str
    """``approve``, ``reject`` or ``done``."""


_APPROVE = re.compile(r"^(?:j'?)?(approuve|valide|accepte|ok pour la proposition)\b")
_REJECT = re.compile(r"^(?:je\s+)?(refuse|rejette|decline)\b")
_DONE = re.compile(
    r"^(c'?est fait|session (?:terminee|finie|faite)|j'?ai (?:fini|termine) (?:ma|la) "
    r"(?:session|seance d'?etude|etude))\b"
)


def parse_action(text: str) -> Action | None:
    plain = _plain(text).rstrip(" !.")
    if _APPROVE.match(plain):
        return Action("approve")
    if _REJECT.match(plain):
        return Action("reject")
    if _DONE.match(plain):
        return Action("done")
    return None


def pending_proposals(session: Session) -> list[ProposalRow]:
    return list(
        session.scalars(
            select(ProposalRow)
            .where(ProposalRow.status == "pending")
            .order_by(ProposalRow.created_at)
        )
    )


def block_to_complete(session: Session, now: datetime, tz: ZoneInfo) -> StudyBlockRow | None:
    version = active_version(session)
    current = StudyBlockRow.locked.is_(True)
    scope = (
        current
        if version is None
        else or_(current, StudyBlockRow.schedule_version_id == version.id)
    )
    day_start = datetime.combine(now.astimezone(tz).date(), time(0), tzinfo=tz)
    candidates = list(
        session.scalars(
            select(StudyBlockRow).where(
                scope,
                StudyBlockRow.state.in_(("planned", "missed")),
                StudyBlockRow.start_at <= now,
                StudyBlockRow.end_at >= day_start,
                StudyBlockRow.end_at <= now + timedelta(days=1),
            )
        )
    )
    underway = [b for b in candidates if b.end_at > now]
    if underway:
        return underway[0]
    ended = [b for b in candidates if b.end_at <= now]
    return max(ended, key=lambda b: b.end_at) if ended else None


def subject_of(session: Session, block: StudyBlockRow) -> str:
    task = session.get(StudyTaskRow, block.task_id)
    return task.subject if task is not None else "?"
