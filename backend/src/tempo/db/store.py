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

from tempo.const import (
    ACCESS_OVERRIDES_SETTING,
    ALTERNATION_SETTING,
    CLASS_ATTENDANCE_SETTING,
    MAX_JOURNEY_SETTING,
    SETTING_DEFAULTS,
    SWITCHES_SETTING,
    TEMPORARY_SETTING,
)
from tempo.db.models import (
    ActivityRow,
    AuditEntry,
    FixedEventRow,
    NotificationRow,
    PlaceAliasRow,
    PlaceRow,
    ProposalRow,
    ScheduleVersion,
    SettingRow,
    StudyBlockRow,
)
from tempo.domain.models import EventStatus
from tempo.transport.access import Access, AccessRouting
from tempo.transport.location import Alternation, DeclaredStay
from tempo.transport.places import Coordinates, Place, PlaceDirectory
from tempo.transport.routing import RoutingProvider


def get_setting(session: Session, key: str, default: Any = None) -> Any:
    """A setting, else ``default``, else its documented default (``SETTING_DEFAULTS``)."""
    fallback = SETTING_DEFAULTS.get(key) if default is None else default
    row = session.get(SettingRow, key)
    value = None if row is None else row.value.get("value")
    return fallback if value is None else value


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


def notify(
    session: Session,
    kind: str,
    title: str,
    message: str,
    dedupe_key: str,
    *,
    due_at: datetime | None = None,
) -> None:
    """Insert a notification once per ``dedupe_key`` (repeated issues do not spam). A
    question raised again while it was resolved is open again (never silently dropped)."""
    stmt = insert(NotificationRow).values(
        id=uuid.uuid4(),
        kind=kind,
        title=title[:300],
        message=message,
        dedupe_key=dedupe_key[:300],
        created_at=datetime.now(UTC),
        due_at=due_at,
    )
    if kind == "question":
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["dedupe_key"],
                set_={"resolved_at": None, "due_at": stmt.excluded.due_at},
            )
        )
    else:
        session.execute(stmt.on_conflict_do_nothing(index_elements=["dedupe_key"]))


def open_questions(session: Session) -> list[NotificationRow]:
    """Unresolved questions of every origin (planning, sources, conversation), most urgent
    first: the earliest needed answer, then the oldest question."""
    return list(
        session.scalars(
            select(NotificationRow)
            .where(NotificationRow.kind == "question", NotificationRow.resolved_at.is_(None))
            .order_by(NotificationRow.due_at.asc().nulls_last(), NotificationRow.created_at)
        )
    )


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


def declared_stays(session: Session, directory: PlaceDirectory) -> list[DeclaredStay]:
    """Planned locations stated in the conversation (temporary exceptions), known places only."""
    return [
        DeclaredStay(place, datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"]))
        for s in get_setting(session, "stays", []) or []
        if (place := directory.get(s["place_id"])) is not None
    ]


def attended_classes(
    session: Session, start: datetime, end: datetime
) -> list[tuple[str, str, datetime, datetime, str | None]]:
    """University events in ``[start, end)`` as the user attends them: ``(ref, title, start,
    end, location)`` with partial attendance applied and skipped occurrences left out."""
    attendance = get_setting(session, CLASS_ATTENDANCE_SETTING, {}) or {}
    found = []
    for event in session.scalars(
        select(FixedEventRow).where(
            FixedEventRow.deleted_at.is_(None),
            FixedEventRow.status != EventStatus.CANCELLED,
            FixedEventRow.end_at > start,
            FixedEventRow.start_at < end,
        )
    ):
        ref = f"event:{event.id}"
        stated = attendance.get(ref, {})
        if stated.get("absent"):
            continue
        arrive, leave = stated.get("arrive_at"), stated.get("leave_at")
        found.append(
            (
                ref,
                event.summary,
                datetime.fromisoformat(arrive) if arrive else event.start_at,
                datetime.fromisoformat(leave) if leave else event.end_at,
                event.location,
            )
        )
    return found


def residence_alternation(
    session: Session, directory: PlaceDirectory, tz: ZoneInfo, now: datetime | None = None
) -> Alternation | None:
    """The recurring residence schedule stated in the conversation, with the change moment of
    each change day resolved: a time the user stated for that date, the time stated for every
    change, else the end of the last attended class that day (unknown otherwise)."""
    raw = get_setting(session, ALTERNATION_SETTING)
    if not raw:
        return None
    places = [directory.get(pid) for pid in raw["residences"]]
    if any(p is None for p in places):
        return None  # a residence was deleted: no schedule rather than a wrong one
    alternation = Alternation(
        tuple(p for p in places if p is not None),
        date.fromisoformat(raw["first_day"]),
        int(raw["period_days"]),
        tz,
        None if not raw.get("switch_at") else time.fromisoformat(raw["switch_at"]),
    )
    moment = now or datetime.now(UTC)
    switches: dict[date, datetime] = {}
    if alternation.switch_at is None:  # after the last attended class of each change day
        for _ref, _title, _start, end, _location in attended_classes(
            session, moment - timedelta(days=alternation.period_days), moment + timedelta(days=62)
        ):
            day = end.astimezone(tz).date()
            if (day - alternation.first_day).days % alternation.period_days == 0:
                switches[day] = max(switches.get(day, end), end, key=lambda x: x.timestamp())
    for day_text, clock in (get_setting(session, SWITCHES_SETTING, {}) or {}).items():
        day = date.fromisoformat(day_text)
        switches[day] = datetime.combine(day, time.fromisoformat(clock), tzinfo=tz)
    return Alternation(
        alternation.residences,
        alternation.first_day,
        alternation.period_days,
        tz,
        alternation.switch_at,
        switches,
    )


def reference_residence(session: Session, directory: PlaceDirectory | None = None) -> Place | None:
    """The first residence of the alternation (daylight reference, geocoding focus)."""
    directory = directory or place_directory(session)
    raw = get_setting(session, ALTERNATION_SETTING) or {}
    first = (raw.get("residences") or [None])[0]
    return directory.get(first) if first else None


def temporary_preferences(session: Session, now: datetime) -> list[dict[str, Any]]:
    """Temporary preferences still in force or to come (expired ones are ignored)."""
    return [
        item
        for item in get_setting(session, TEMPORARY_SETTING, []) or []
        if datetime.fromisoformat(item["end"]) > now
    ]


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
    limit = timedelta(minutes=int(get_setting(session, MAX_JOURNEY_SETTING)))
    return AccessRouting(routing, accesses, max_journey=limit)
