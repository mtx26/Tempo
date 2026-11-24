"""Database side of planning: load inputs, run the engine, persist the outcome.

Persistence rules:
- Ordinary movable study blocks are re-optimised automatically: a valid outcome becomes the new
  ``active`` schedule version, the previous one is ``superseded`` (kept for history).
- Locked study blocks (``study_block.locked``, version NULL) are protected: they are never moved.
  A fixed commitment colliding with one creates a ``proposal`` (kind ``unlock_block``); only the
  user's approval unlocks it (ARCHITECTURE.md: protected changes need approval).
- An ``invalid`` or ``no_solution`` outcome never replaces the active plan; it is recorded as a
  ``rejected`` version and notified.
- Issues (impossible journeys, unplanned hard deadlines, unknown locations...) become
  de-duplicated notifications for Home Assistant.
"""

import hashlib
import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tempo.db.models import (
    ActivityRow,
    AuditEntry,
    AvailabilityExceptionRow,
    AvailabilityRow,
    CategoryWindowRow,
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
from tempo.domain.models import EventStatus, TargetKind
from tempo.domain.ops import needs_session, remaining_minutes
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
ACTIVITY_KINDS = {"race": CommitmentKind.RACE, "training": CommitmentKind.TRAINING}
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
    return _digest("#".join(parts)) if parts else None


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
    """Weekly windows, then the dated exceptions of each day: blocked time is removed from
    the windows; extra free time is added (at its place). Fixed commitments still win."""
    rows = list(session.scalars(select(AvailabilityRow)))
    first_day = start.astimezone(tz).date()
    exceptions = list(
        session.scalars(
            select(AvailabilityExceptionRow).where(
                AvailabilityExceptionRow.day >= first_day - timedelta(days=1),
                AvailabilityExceptionRow.day <= end.astimezone(tz).date(),
            )
        )
    )
    windows: list[StudyWindow] = []
    day = first_day
    while datetime.combine(day, time(0), tzinfo=tz) < end:
        midnight = datetime.combine(day, time(0), tzinfo=tz)
        todays: list[StudyWindow] = []
        for row in rows:
            place = directory.get(row.place_id)
            if row.weekday != day.weekday() or place is None:
                continue
            begin, finish = _wall_clock(day, row.start_minute, row.end_minute, tz)
            if utc(finish) > utc(begin) and utc(midnight) < utc(end):
                todays.append(StudyWindow(Interval(begin, finish), place))
        for exc in (e for e in exceptions if e.day == day):
            begin, finish = _wall_clock(day, exc.start_minute, exc.end_minute, tz)
            if utc(finish) <= utc(begin):
                continue
            if exc.available:
                place = directory.get(exc.place_id) if exc.place_id else None
                if place is not None:
                    todays.append(StudyWindow(Interval(begin, finish), place))
            else:
                todays = _remove(todays, Interval(begin, finish))
        windows += todays
        day += timedelta(days=1)
    return windows


def _wall_clock(day: date, start: int, end: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    # Wall-clock minutes (DST-safe): build local times, not midnight + offset.
    begin = datetime.combine(day, time(start // 60, start % 60), tzinfo=tz)
    finish = (
        _end_of_day(day, tz)
        if end == 1440
        else datetime.combine(day, time(end // 60, end % 60), tzinfo=tz)
    )
    return begin, finish


def _remove(windows: list[StudyWindow], blocked: Interval) -> list[StudyWindow]:
    kept: list[StudyWindow] = []
    for window in windows:
        span = window.interval
        if not span.overlaps(blocked):
            kept.append(window)
            continue
        if utc(span.start) < utc(blocked.start):
            kept.append(StudyWindow(Interval(span.start, blocked.start), window.place))
        if utc(blocked.end) < utc(span.end):
            kept.append(StudyWindow(Interval(blocked.end, span.end), window.place))
    return kept


@dataclass(frozen=True, slots=True)
class LoadedInput:
    data: PlanningInput
    task_ids: dict[str, uuid.UUID]
    spread: dict[str, int] = field(default_factory=dict)
    """Tasks whose target is beyond the horizon -> minutes planned in this horizon."""


SHARE_GRANULARITY = 15


def _proportional_share(remaining: int, today: date, last_day: date, target: date) -> int:
    window = (last_day - today).days + 1
    total = (target - today).days + 1
    share = math.ceil(remaining * window / total / SHARE_GRANULARITY) * SHARE_GRANULARITY
    return min(remaining, max(share, SHARE_GRANULARITY))


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
    activity_rows = list(activities)
    category_rows = list(session.scalars(select(CategoryWindowRow)))
    activities_by_group: dict[str, list[ActivityRow]] = {}
    for act in activity_rows:
        if act.sequence_group is not None:
            activities_by_group.setdefault(act.sequence_group, []).append(act)
    for act in activity_rows:
        if act.duration_minutes is None:
            continue  # duration to confirm: never scheduled with a guessed length
        if act.all_day and not act.busy:
            continue  # informative all-day event: shown, blocks nothing
        travels = act.place_id is not None and not (act.remote or act.all_day)
        place = directory.get(act.place_id) if travels and act.place_id else None
        duration = timedelta(minutes=act.duration_minutes)
        fixed_start = (
            act.earliest_start_at
            if act.scheduling == "fixed"
            else (act.planned_start_at or act.preferred_start_at)
            if act.locked
            else None
        )
        if fixed_start is not None:
            kind = ACTIVITY_KINDS.get(act.kind, CommitmentKind.OTHER)
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
                    act.preferred_day,
                    act.sequence_group,
                    act.sequence_index or 0,
                    _recovery_days(act, activities_by_group),
                    _category_allowed(category_rows, act, tz),
                )
            )

    task_ids: dict[str, uuid.UUID] = {}
    demands: list[StudyDemand] = []
    spread: dict[str, int] = {}
    today = now.astimezone(tz).date()
    last_day = (horizon_end - timedelta(minutes=1)).astimezone(tz).date()
    for task in session.scalars(select(StudyTaskRow).where(StudyTaskRow.archived.is_(False))):
        remaining = remaining_minutes(task)
        if not remaining or needs_session(task):
            # Unknown duration: kept, never planned with a guessed length. Located or
            # non-splittable tasks are planned as one flexible session (an activity).
            continue
        task_ids[str(task.id)] = task.id
        if task.target_date is None:
            # No deadline: low urgency, spread like a far target (never forced in).
            target_day = last_day + timedelta(days=DEFAULT_HORIZON_DAYS)
            target_kind = TargetKind.SOFT
        else:
            target_day, target_kind = task.target_date, task.target_kind
        target, minutes = _end_of_day(target_day, tz), remaining
        if target_day > last_day:
            # Target beyond the planning horizon: plan only the proportional share now (spread
            # the work until the target); the real deadline is checked once it is in range.
            minutes = _proportional_share(remaining, today, last_day, target_day)
            target, target_kind = _end_of_day(last_day, tz), TargetKind.SOFT
            spread[str(task.id)] = minutes
        demands.append(
            StudyDemand(
                str(task.id), task.subject, minutes, target, target_kind, priority=task.priority
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
    return LoadedInput(data, task_ids, spread)


def _category_allowed(
    rows: list[CategoryWindowRow], act: ActivityRow, tz: ZoneInfo
) -> tuple[Interval, ...] | None:
    """Category windows covering the activity's window (B07), or None if unrestricted."""
    mine = [r for r in rows if r.kind == act.kind]
    if not mine:
        return None
    allowed: list[Interval] = []
    day = act.earliest_start_at.astimezone(tz).date()
    last = act.latest_end_at.astimezone(tz).date()
    while day <= last:
        for row in mine:
            if row.weekday == day.weekday():
                begin, finish = _wall_clock(day, row.start_minute, row.end_minute, tz)
                if utc(finish) > utc(begin):
                    allowed.append(Interval(begin, finish))
        day += timedelta(days=1)
    return tuple(allowed)


MAX_RECOVERY_DAYS = 2


def _recovery_days(act: ActivityRow, groups: dict[str, list[ActivityRow]]) -> int:
    """Calendar days to keep after the previous session of the same coach week: the gap the
    coach planned between the two suggested days, capped at ``MAX_RECOVERY_DAYS``."""
    if act.sequence_group is None or act.preferred_day is None:
        return 0
    earlier = [
        other
        for other in groups.get(act.sequence_group, [])
        if other.preferred_day is not None
        and (other.sequence_index or 0) < (act.sequence_index or 0)
    ]
    if not earlier:
        return 0
    previous = max(earlier, key=lambda other: other.sequence_index or 0)
    assert previous.preferred_day is not None  # noqa: S101 - filtered above
    return min(max((act.preferred_day - previous.preferred_day).days, 0), MAX_RECOVERY_DAYS)


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
    _activity_conflicts(session, data)


def _activity_conflicts(session: Session, data: PlanningInput) -> None:
    """Protected activities colliding with a university event (iCal is the source of truth).

    - locked training: proposal to unlock it (approval -> it becomes flexible and is re-placed);
    - race (fixed by nature): nothing can move automatically -> conflict notification."""
    classes = [c for c in data.commitments if c.kind is CommitmentKind.CLASS]
    protected = [
        c
        for c in data.commitments
        if c.kind in (CommitmentKind.RACE, CommitmentKind.TRAINING, CommitmentKind.OTHER)
    ]
    for activity in protected:
        span = Interval(activity.start, activity.end)
        for event in classes:
            if not Interval(event.start, event.end).overlaps(span):
                continue
            key = f"{activity.ref}:{event.ref}:{event.start.isoformat()}"
            when = event.start.isoformat(timespec="minutes")
            if activity.kind is CommitmentKind.TRAINING:
                propose(
                    session,
                    "unlock_activity",
                    f"Le cours '{event.title}' chevauche l'entraînement verrouillé "
                    f"'{activity.title}'",
                    {
                        "activity_id": activity.ref.removeprefix("activity:"),
                        "commitment": event.ref,
                        "commitment_start": event.start.isoformat(),
                    },
                    f"unlock_activity:{key}",
                )
            else:
                race = activity.kind is CommitmentKind.RACE
                notify(
                    session,
                    "race_conflict" if race else "fixed_conflict",
                    "Conflit avec une compétition" if race else "Conflit avec un rendez-vous",
                    f"Le cours '{event.title}' ({when}) chevauche "
                    f"{'la compétition' if race else 'le rendez-vous fixe'} "
                    f"'{activity.title}'. Rien n'est déplacé automatiquement : à arbitrer.",
                    f"race_conflict:{key}",
                )


def mark_missed_blocks(session: Session, now: datetime) -> int:
    """Past blocks never reported done become ``missed`` (requirements: track missed sessions).

    Their effort was never added to ``completed_minutes``, so the re-plan places it again
    automatically; the user is told once per block."""
    # Only blocks of the CURRENT plan (or locked ones): superseded versions keep their rows for
    # history, but those positions were replaced and must never be reported as missed.
    active = active_version(session)
    current: ColumnElement[bool] = StudyBlockRow.locked.is_(True)
    if active is not None:
        current = or_(current, StudyBlockRow.schedule_version_id == active.id)
    rows = list(
        session.scalars(
            select(StudyBlockRow).where(
                StudyBlockRow.state == "planned",
                StudyBlockRow.end_at <= now,
                current,
            )
        )
    )
    subjects = {t.id: t.subject for t in session.scalars(select(StudyTaskRow))}
    for row in rows:
        row.state = "missed"
        notify(
            session,
            "missed_block",
            "Session d'étude manquée",
            f"La session « {subjects.get(row.task_id, '?')} » du "
            f"{row.start_at.isoformat(timespec='minutes')} n'a pas été marquée comme faite : "
            "son temps est replanifié. Marquez-la comme faite si vous l'avez réalisée.",
            f"missed:{row.id}",
        )
    return len(rows)


def _underway_blocks(session: Session, now: datetime, loaded: LoadedInput) -> list[PlannedBlock]:
    active = active_version(session)
    if active is None:
        return []
    subjects = {d.task_id: d.subject for d in loaded.data.demands}
    rows = session.scalars(
        select(StudyBlockRow).where(
            StudyBlockRow.schedule_version_id == active.id,
            StudyBlockRow.locked.is_(False),
            StudyBlockRow.state == "planned",
            StudyBlockRow.start_at <= now,
            StudyBlockRow.end_at > now,
        )
    )
    return [
        PlannedBlock(str(r.task_id), subjects[str(r.task_id)], r.start_at, r.end_at, locked=True)
        for r in rows
        if str(r.task_id) in subjects
    ]


REPLAN_LOCK = 0x7E4D0
"""PostgreSQL advisory lock: the API and the worker never re-plan concurrently (the second
waits, then plans on the first one's published version; no silent overwrite)."""


def _block_keys(session: Session, version: ScheduleVersion | None) -> set[tuple[str, str, str]]:
    if version is None:
        return set()
    return {
        (str(b.task_id), utc(b.start_at).isoformat(), utc(b.end_at).isoformat())
        for b in session.scalars(
            select(StudyBlockRow).where(
                StudyBlockRow.schedule_version_id == version.id,
                StudyBlockRow.locked.is_(False),
            )
        )
    }


def _schedule_diff(
    before: set[tuple[str, str, str]],
    after: set[tuple[str, str, str]],
    moved_activities: list[str],
) -> dict[str, Any]:
    removed, added = before - after, after - before
    shift = 0
    moved = 0
    pending = sorted(added)
    for task, start, _end in sorted(removed):
        same = next((a for a in pending if a[0] == task), None)
        if same is None:
            continue
        pending.remove(same)
        moved += 1
        shift += abs(
            int(
                (datetime.fromisoformat(same[1]) - datetime.fromisoformat(start)).total_seconds()
                // 60
            )
        )
    return {
        "added_blocks": len(added) - moved,
        "removed_blocks": len(removed) - moved,
        "moved_blocks": moved,
        "kept_blocks": len(before & after),
        "total_shift_minutes": shift,
        "moved_activities": moved_activities,
    }


def replan(
    session: Session,
    routing: RoutingProvider,
    now: datetime,
    tz: ZoneInfo,
    *,
    reason: str,
    mode: str = "local",
) -> ScheduleVersion:
    """Compute and publish a new plan. ``mode = "global"`` (explicit user request only) drops
    the stability preference so the whole horizon may be reorganised; locked and under-way
    items stay protected in every mode."""
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": REPLAN_LOCK})
    missed = mark_missed_blocks(session, now)
    loaded = build_input(session, now, tz)
    if mode == "global":
        loaded = LoadedInput(
            replace(
                loaded.data,
                previous_blocks=[],
                trainings=[replace(t, previous_start=None) for t in loaded.data.trainings],
            ),
            loaded.task_ids,
            loaded.spread,
        )
    before_blocks = _block_keys(session, active_version(session))
    before_activities = {t.activity_id: t.previous_start for t in loaded.data.trainings}
    underway = _underway_blocks(session, now, loaded)
    if underway:
        # A session already under way is never moved (requirements): fixed for this re-plan,
        # then carried as-is into the new version.
        loaded = LoadedInput(
            replace(loaded.data, locked_blocks=[*loaded.data.locked_blocks, *underway]),
            loaded.task_ids,
            loaded.spread,
        )
    carried = {(b.task_id, utc(b.start)) for b in underway}
    outcome = run_planning(loaded.data, routing)
    result = outcome.result
    summary: dict[str, Any] = {
        "missed_blocks": missed,
        "spread": loaded.spread,
        "reason": reason,
        "mode": mode,
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
        "deadline_capacity": outcome.deadline_capacity,
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
            if block.locked and (block.task_id, utc(block.start)) not in carried:
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

    if usable:
        session.flush()
        moved = [
            placed.activity_id
            for placed in outcome.trainings
            if before_activities.get(placed.activity_id) is not None
            and utc(before_activities[placed.activity_id]) != utc(placed.start)  # type: ignore[arg-type]
        ]
        summary["diff"] = _schedule_diff(before_blocks, _block_keys(session, version), moved)
        version.summary = dict(summary)
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
