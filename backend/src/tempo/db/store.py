"""Shared persistence services used by every package (API, assistant, planning, sync, worker).

Small and dependency-free on purpose (database models only), so that synchronisation and
interpretation code never depends on the planning pipeline:
- settings (``app_setting``): ``get_setting`` / ``set_setting``;
- audit trail (``audit_entry``, append-only for the runtime role): ``audit``;
- notifications, created by ONE function, de-duplicated by key: ``notify``;
- proposals for protected changes, stamped with their input revision: ``propose``;
- read helpers: the active schedule version, the place directory.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tempo.db.models import (
    ActivityRow,
    AuditEntry,
    NotificationRow,
    PlaceAliasRow,
    PlaceRow,
    ProposalRow,
    ScheduleVersion,
    SettingRow,
    StudyBlockRow,
)
from tempo.transport.places import Coordinates, Place, PlaceDirectory


def get_setting(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(SettingRow, key)
    return default if row is None else row.value.get("value", default)


def set_setting(session: Session, key: str, value: Any) -> None:
    stmt = insert(SettingRow).values(key=key, value={"value": value})
    session.execute(
        stmt.on_conflict_do_update(index_elements=["key"], set_={"value": stmt.excluded.value})
    )


def audit(
    session: Session, actor: str, action: str, entity: str, entity_id: str, **details: Any
) -> None:
    session.add(
        AuditEntry(
            at=datetime.now(UTC),
            actor=actor,
            action=action,
            entity=entity,
            entity_id=entity_id,
            details=details,
        )
    )


def digest(text: str) -> str:
    """Short stable digest (de-duplication keys, proposal revisions)."""
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def notify(session: Session, kind: str, title: str, message: str, dedupe_key: str) -> None:
    """Insert a notification once per ``dedupe_key`` (repeated issues do not spam)."""
    stmt = insert(NotificationRow).values(
        id=uuid.uuid4(),
        kind=kind,
        title=title[:300],
        message=message,
        dedupe_key=dedupe_key[:300],
        created_at=datetime.now(UTC),
    )
    session.execute(stmt.on_conflict_do_nothing(index_elements=["dedupe_key"]))


def proposal_revision(session: Session, details: dict[str, Any]) -> str | None:
    """Digest of the state a proposal was computed on (its target block / activity). Approval
    recomputes it: a different value means the inputs changed and the proposal is obsolete."""
    parts: list[str] = []
    if "block_id" in details:
        block = session.get(StudyBlockRow, uuid.UUID(details["block_id"]))
        parts.append(
            "-"
            if block is None
            else f"{block.start_at}|{block.end_at}|{block.locked}|{block.state}"
        )
    if "activity_id" in details:
        act = session.get(ActivityRow, uuid.UUID(details["activity_id"]))
        parts.append(
            "-"
            if act is None
            else f"{act.status}|{act.locked}|{act.earliest_start_at}|{act.latest_end_at}|"
            f"{act.duration_minutes}|{act.planned_start_at if act.locked else ''}"
        )
    return digest("#".join(parts)) if parts else None


def propose(
    session: Session, kind: str, title: str, details: dict[str, Any], dedupe_key: str
) -> None:
    stmt = insert(ProposalRow).values(
        id=uuid.uuid4(),
        kind=kind,
        title=title[:300],
        details=details,
        dedupe_key=dedupe_key[:300],
        created_at=datetime.now(UTC),
        input_revision=proposal_revision(session, details),
    )
    session.execute(stmt.on_conflict_do_nothing(index_elements=["dedupe_key"]))


def active_version(session: Session) -> ScheduleVersion | None:
    return session.scalars(
        select(ScheduleVersion)
        .where(ScheduleVersion.state == "active")
        .order_by(ScheduleVersion.created_at.desc())
    ).first()


def place_directory(session: Session) -> PlaceDirectory:
    places = [
        Place(p.id, p.name, Coordinates(p.latitude, p.longitude))
        for p in session.scalars(select(PlaceRow))
    ]
    aliases = {a.alias: a.place_id for a in session.scalars(select(PlaceAliasRow))}
    return PlaceDirectory(places, aliases)
