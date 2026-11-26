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
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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
from tempo.transport.access import DEFAULT_MAX_JOURNEY, Access, AccessRouting
from tempo.transport.location import Alternation, DeclaredStay
from tempo.transport.places import Coordinates, Place, PlaceDirectory
from tempo.transport.routing import RoutingProvider


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


ALTERNATION_SETTING = "residence_alternation"
ACCESS_OVERRIDES_SETTING = "access_overrides"
"""Temporary transport access points for a period (never the persistent default)."""
MAX_JOURNEY_SETTING = "max_journey_minutes"


def declared_stays(session: Session, directory: PlaceDirectory) -> list[DeclaredStay]:
    """Planned locations stated in the conversation (temporary exceptions), known places only."""
    return [
        DeclaredStay(place, datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"]))
        for s in get_setting(session, "stays", []) or []
        if (place := directory.get(s["place_id"])) is not None
    ]


def residence_alternation(
    session: Session, directory: PlaceDirectory, tz: ZoneInfo
) -> Alternation | None:
    """The recurring residence schedule stated in the conversation, if any."""
    raw = get_setting(session, ALTERNATION_SETTING)
    if not raw:
        return None
    places = [directory.get(pid) for pid in raw["residences"]]
    if any(p is None for p in places):
        return None  # a residence was deleted: no schedule rather than a wrong one
    return Alternation(
        tuple(p for p in places if p is not None),
        date.fromisoformat(raw["first_day"]),
        time.fromisoformat(raw["switch_at"]),
        int(raw["period_days"]),
        tz,
    )


def residence_base(session: Session, directory: PlaceDirectory | None = None) -> Place | None:
    """Fallback base: the first residence of the alternation (one residence is an alternation
    of one place); None when no residence is configured."""
    directory = directory or place_directory(session)
    raw = get_setting(session, ALTERNATION_SETTING) or {}
    first = (raw.get("residences") or [None])[0]
    return directory.get(first) if first else None


def access_routing(
    session: Session, routing: RoutingProvider, temporary: list[Access] | None = None
) -> AccessRouting:
    """The routing provider with the user's default transport access points and journey limit.
    ``temporary`` accesses (stated for one journey only, never saved) take precedence."""
    directory = place_directory(session)
    accesses = list(temporary or [])
    for item in get_setting(session, ACCESS_OVERRIDES_SETTING, []) or []:
        place, point = directory.get(item["place_id"]), directory.get(item["access_place_id"])
        if place is not None and point is not None:
            accesses.append(
                Access(
                    place,
                    point,
                    timedelta(minutes=int(item["access_minutes"])),
                    datetime.fromisoformat(item["start"]),
                    datetime.fromisoformat(item["end"]),
                )
            )
    for row in session.scalars(select(PlaceRow).where(PlaceRow.access_place_id.is_not(None))):
        place, point = directory.get(row.id), directory.get(row.access_place_id or "")
        if place is not None and point is not None and row.access_minutes:
            accesses.append(Access(place, point, timedelta(minutes=row.access_minutes)))
    minutes = get_setting(session, MAX_JOURNEY_SETTING)
    limit = DEFAULT_MAX_JOURNEY if not minutes else timedelta(minutes=int(minutes))
    return AccessRouting(routing, accesses, max_journey=limit)
