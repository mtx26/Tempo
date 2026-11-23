"""Database side of planning: load inputs, run the engine, persist the outcome.

Persistence rules:
- Ordinary movable study blocks are re-optimised automatically: a valid outcome becomes the new
  ``active`` schedule version, the previous one is ``superseded`` (kept for history).
- Locked study blocks (``study_block.locked``, version NULL) are protected: they are never moved.
  A fixed commitment colliding with one creates a ``proposal`` (kind ``unlock_block``); only the
  user's approval unlocks it (docs/REQUIREMENTS.md: protected changes need approval).
- An ``invalid`` or ``no_solution`` outcome never replaces the active plan; it is recorded as a
  ``rejected`` version and notified.
- Issues (impossible journeys, unplanned hard deadlines, unknown locations...) become
  de-duplicated notifications for Home Assistant.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tempo.db.models import (
    ActivityRow,
    AuditEntry,
    AvailabilityRow,
    FixedEventRow,
    NotificationRow,
    PlaceAliasRow,
    PlaceRow,
    ProposalRow,
    ScheduleVersion,
    SettingRow,
    StudyBlockRow,
    StudyTaskRow,
)
from tempo.domain.models import EventStatus
from tempo.planning.engine import (
    Commitment,
    CommitmentKind,
    OutcomeStatus,
    PlanningInput,
    PlanningOutcome,
    TrainingRequest,
    run_planning,
)
from tempo.scheduler.model import Interval, PlannedBlock, Preferences, StudyDemand, utc
from tempo.transport.location import DeclaredStay
from tempo.transport.places import Coordinates, Place, PlaceDirectory
from tempo.transport.routing import RoutingProvider
from tempo.transport.travel import StudyWindow

DEFAULT_HORIZON_DAYS = 14
ACTOR = "planner"


class PlanningNotConfiguredError(RuntimeError):
    """A prerequisite (home place) is missing: Tempo asks instead of guessing."""


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
    )
    session.execute(stmt.on_conflict_do_nothing(index_elements=["dedupe_key"]))


def _place(row: PlaceRow) -> Place:
    return Place(row.id, row.name, Coordinates(row.latitude, row.longitude))


def place_directory(session: Session) -> PlaceDirectory:
    places = [_place(p) for p in session.scalars(select(PlaceRow))]
    aliases = {a.alias: a.place_id for a in session.scalars(select(PlaceAliasRow))}
    return PlaceDirectory(places, aliases)


def _end_of_day(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)


def _availability(
    session: Session, directory: PlaceDirectory, start: datetime, end: datetime, tz: ZoneInfo
) -> list[StudyWindow]:
    rows = list(session.scalars(select(AvailabilityRow)))
    windows: list[StudyWindow] = []
    day = start.astimezone(tz).date()
    while datetime.combine(day, time(0), tzinfo=tz) < end:
        for row in rows:
            place = directory.get(row.place_id)
            if row.weekday != day.weekday() or place is None:
                continue
            midnight = datetime.combine(day, time(0), tzinfo=tz)
            # Wall-clock minutes (DST-safe): build local times, not midnight + offset.
            begin = datetime.combine(
                day, time(row.start_minute // 60, row.start_minute % 60), tzinfo=tz
            )
            finish = (
                _end_of_day(day, tz)
                if row.end_minute == 1440
                else datetime.combine(
                    day, time(row.end_minute // 60, row.end_minute % 60), tzinfo=tz
                )
            )
            if utc(finish) > utc(begin) and utc(midnight) < utc(end):
                windows.append(StudyWindow(Interval(begin, finish), place))
        day += timedelta(days=1)
    return windows


@dataclass(frozen=True, slots=True)
class LoadedInput:
    data: PlanningInput
    task_ids: dict[str, uuid.UUID]


def build_input(session: Session, now: datetime, tz: ZoneInfo) -> LoadedInput:
    directory = place_directory(session)
    home_id = get_setting(session, "home_place_id")
    home = directory.get(home_id) if home_id else None
    if home is None:
        raise PlanningNotConfiguredError("no home place configured (setting home_place_id)")
    horizon_days = int(get_setting(session, "horizon_days", DEFAULT_HORIZON_DAYS))
    horizon_end = _end_of_day((now + timedelta(days=horizon_days)).astimezone(tz).date(), tz)
    buffer = timedelta(minutes=int(get_setting(session, "buffer_minutes", 10)))

    commitments: list[Commitment] = []
    events = session.scalars(
        select(FixedEventRow).where(
            FixedEventRow.deleted_at.is_(None),
            FixedEventRow.status != EventStatus.CANCELLED,
            FixedEventRow.end_at > now,
            FixedEventRow.start_at < horizon_end,
        )
    )
    for event in events:
        commitments.append(
            Commitment(
                f"event:{event.id}",
                event.summary,
                event.start_at,
                event.end_at,
                CommitmentKind.CLASS,
                directory.resolve(event.location),
                event.location,
            )
        )

    trainings: list[TrainingRequest] = []
    activities = session.scalars(
        select(ActivityRow).where(
            ActivityRow.status == "active",
            ActivityRow.latest_end_at > now,
            ActivityRow.earliest_start_at < horizon_end,
        )
    )
    for act in activities:
        place = directory.get(act.place_id) if act.place_id else None
        duration = timedelta(minutes=act.duration_minutes)
        fixed_start = (
            act.earliest_start_at
            if act.kind == "race"
            else (act.planned_start_at or act.preferred_start_at)
            if act.locked
            else None
        )
        if fixed_start is not None:
            kind = CommitmentKind.RACE if act.kind == "race" else CommitmentKind.TRAINING
            commitments.append(
                Commitment(
                    f"activity:{act.id}",
                    act.title,
                    fixed_start,
                    fixed_start + duration,
                    kind,
                    place,
                )
            )
        else:
            trainings.append(
                TrainingRequest(
                    str(act.id),
                    act.title,
                    duration,
                    act.earliest_start_at,
                    act.latest_end_at,
                    place,
                    act.preferred_start_at,
                    act.planned_start_at,
                )
            )

    task_ids: dict[str, uuid.UUID] = {}
    demands: list[StudyDemand] = []
    for task in session.scalars(select(StudyTaskRow).where(StudyTaskRow.archived.is_(False))):
        remaining = max(0, task.effort_minutes - task.completed_minutes)
        if remaining == 0:
            continue
        task_ids[str(task.id)] = task.id
        demands.append(
            StudyDemand(
                str(task.id),
                task.subject,
                remaining,
                _end_of_day(task.target_date, tz),
                task.target_kind,
            )
        )
    subjects = {d.task_id: d.subject for d in demands}

    locked = [
        PlannedBlock(
            str(b.task_id), subjects.get(str(b.task_id), "?"), b.start_at, b.end_at, locked=True
        )
        for b in session.scalars(
            select(StudyBlockRow).where(StudyBlockRow.locked.is_(True), StudyBlockRow.end_at > now)
        )
        if str(b.task_id) in subjects
    ]
    active = active_version(session)
    previous: list[PlannedBlock] = []
    if active is not None:
        previous = [
            PlannedBlock(str(b.task_id), subjects[str(b.task_id)], b.start_at, b.end_at)
            for b in session.scalars(
                select(StudyBlockRow).where(
                    StudyBlockRow.schedule_version_id == active.id,
                    StudyBlockRow.locked.is_(False),
                )
            )
            if str(b.task_id) in subjects
        ]

    stays = [
        DeclaredStay(
            directory.get(s["place_id"]) or home,
            datetime.fromisoformat(s["start"]),
            datetime.fromisoformat(s["end"]),
        )
        for s in get_setting(session, "stays", [])
        if directory.get(s["place_id"]) is not None
    ]
    prefs = get_setting(session, "study_preferences", {}) or {}
    data = PlanningInput(
        now=now,
        horizon_end=horizon_end,
        timezone=tz,
        home=home,
        commitments=commitments,
        trainings=trainings,
        availability=_availability(session, directory, now, horizon_end, tz),
        demands=demands,
        locked_blocks=locked,
        previous_blocks=previous,
        stays=stays,
        preferences=Preferences(**prefs),
        buffer=buffer,
    )
    return LoadedInput(data, task_ids)


def active_version(session: Session) -> ScheduleVersion | None:
    return session.scalars(
        select(ScheduleVersion)
        .where(ScheduleVersion.state == "active")
        .order_by(ScheduleVersion.created_at.desc())
    ).first()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def _travel_json(outcome: PlanningOutcome) -> list[dict[str, Any]]:
    items = []
    for block in outcome.travel:
        journey = block.journey
        items.append(
            {
                "start": block.start.isoformat(),
                "end": block.end.isoformat(),
                "origin": block.origin.id,
                "destination": block.destination.id,
                "origin_name": block.origin.name,
                "destination_name": block.destination.name,
                "status": block.status.value,
                "departure": None if journey is None else journey.departure.isoformat(),
                "arrival": None if journey is None else journey.arrival.isoformat(),
                "legs": []
                if journey is None
                else [
                    {
                        "mode": leg.mode,
                        "line": leg.line,
                        "agency": leg.agency,
                        "from": leg.origin_name,
                        "to": leg.destination_name,
                        "start": leg.start.isoformat(),
                        "end": leg.end.isoformat(),
                        "realtime": leg.realtime,
                    }
                    for leg in journey.legs
                ],
            }
        )
    return items


def _conflict_proposals(session: Session, data: PlanningInput) -> None:
    fixed = [Interval(c.start, c.end) for c in data.commitments]
    rows = session.scalars(
        select(StudyBlockRow).where(StudyBlockRow.locked.is_(True), StudyBlockRow.end_at > data.now)
    )
    for row in rows:
        block = Interval(row.start_at, row.end_at)
        for commitment, interval in zip(data.commitments, fixed, strict=True):
            if interval.overlaps(block):
                propose(
                    session,
                    "unlock_block",
                    f"'{commitment.title}' chevauche un bloc d'étude verrouillé",
                    {
                        "block_id": str(row.id),
                        "block_start": row.start_at.isoformat(),
                        "commitment": commitment.ref,
                        "commitment_start": commitment.start.isoformat(),
                    },
                    f"unlock:{row.id}:{commitment.ref}:{commitment.start.isoformat()}",
                )


def replan(
    session: Session, routing: RoutingProvider, now: datetime, tz: ZoneInfo, *, reason: str
) -> ScheduleVersion:
    loaded = build_input(session, now, tz)
    outcome = run_planning(loaded.data, routing)
    result = outcome.result
    summary: dict[str, Any] = {
        "reason": reason,
        "planned_at": now.isoformat(),
        "horizon_end": loaded.data.horizon_end.isoformat(),
        "status": outcome.status.value,
        "issues": outcome.issues,
        "warnings": outcome.warnings,
        "violations": outcome.violations,
        "unplaced_trainings": outcome.unplaced_trainings,
        "travel": _travel_json(outcome),
        "unplanned_minutes": {} if result is None else result.unplanned_minutes,
        "late_minutes": {} if result is None else result.late_minutes,
        "hard_deadline_misses": [] if result is None else result.hard_deadline_misses,
        "conflicts": [] if result is None else result.conflicts,
    }
    usable = outcome.status in (OutcomeStatus.COMPLETE, OutcomeStatus.PARTIAL)
    version = ScheduleVersion(
        state="active" if usable else "rejected",
        solver_status=(result.solver_status if result else "")[:20],
        summary=summary,
    )
    if usable:
        session.execute(
            update(ScheduleVersion)
            .where(ScheduleVersion.state == "active")
            .values(state="superseded")
        )
    session.add(version)
    session.flush()

    if usable and result is not None:
        for block in result.blocks:
            if block.locked:
                continue
            session.add(
                StudyBlockRow(
                    task_id=loaded.task_ids[block.task_id],
                    schedule_version_id=version.id,
                    start_at=block.start,
                    end_at=block.end,
                )
            )
        for placed in outcome.trainings:
            session.execute(
                update(ActivityRow)
                .where(ActivityRow.id == uuid.UUID(placed.activity_id))
                .values(planned_start_at=placed.start)
            )
        for missing in outcome.unplaced_trainings:
            session.execute(
                update(ActivityRow)
                .where(ActivityRow.id == uuid.UUID(missing))
                .values(planned_start_at=None)
            )
    else:
        reason_text = "; ".join(outcome.violations) or (
            f"aucune solution ({result.solver_status})" if result else "aucune solution"
        )
        notify(
            session,
            "plan_rejected",
            "Planification impossible à valider",
            f"Le nouveau planning n'a pas été appliqué (l'ancien reste actif) : {reason_text}",
            f"rejected:{_digest(reason_text)}:{now.date()}",
        )

    _conflict_proposals(session, loaded.data)
    for issue in outcome.issues:
        notify(session, "plan_issue", "Tempo : point à vérifier", issue, f"issue:{_digest(issue)}")
    if result is not None:
        for task in result.hard_deadline_misses:
            notify(
                session,
                "deadline",
                "Échéance impossible à tenir",
                f"Le travail de la tâche {task} ne tient pas avant son échéance stricte "
                f"({result.unplanned_minutes.get(task, 0)} min non planifiées).",
                f"deadline:{task}:{result.unplanned_minutes.get(task, 0)}",
            )
    audit(
        session,
        ACTOR,
        "replan",
        "schedule_version",
        str(version.id),
        reason=reason,
        status=outcome.status.value,
    )
    return version
