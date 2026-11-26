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

import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, or_, select, text, update
from sqlalchemy.orm import Session

from tempo.db.models import (
    ActivityRow,
    AvailabilityExceptionRow,
    AvailabilityRow,
    CategoryWindowRow,
    FixedEventRow,
    ScheduleVersion,
    StudyBlockRow,
    StudyTaskRow,
)
from tempo.db.store import (
    access_routing,
    active_version,
    audit,
    declared_stays,
    digest,
    get_setting,
    notify,
    place_directory,
    propose,
    residence_alternation,
    residence_base,
)
from tempo.domain.models import EventStatus, TargetKind
from tempo.domain.ops import needs_session, refresh_task_sessions, remaining_minutes
from tempo.planning.engine import (
    DEFAULT_ACTIVITY_HOURS,
    Commitment,
    CommitmentKind,
    Conflict,
    ConflictKind,
    OutcomeStatus,
    PlanningInput,
    PlanningOutcome,
    TrainingRequest,
    run_planning,
)
from tempo.scheduler.activities import TrainingPreferences
from tempo.scheduler.model import PlannedBlock, Preferences, StudyDemand
from tempo.timeutil import Interval, end_of_day, utc
from tempo.transport.places import PlaceDirectory
from tempo.transport.routing import RoutingProvider
from tempo.transport.travel import StudyWindow

DEFAULT_HORIZON_DAYS = 14
ACTIVITY_KINDS = {"race": CommitmentKind.RACE, "training": CommitmentKind.TRAINING}
ACTOR = "planner"


class PlanningNotConfiguredError(RuntimeError):
    """A prerequisite (home place) is missing: Tempo asks instead of guessing."""


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
        end_of_day(day, tz)
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
    home = residence_base(session, directory)
    if home is None:
        raise PlanningNotConfiguredError(
            "no residence configured (say where you live: set_residence_alternation)"
        )
    horizon_days = int(get_setting(session, "horizon_days", DEFAULT_HORIZON_DAYS))
    horizon_end = end_of_day((now + timedelta(days=horizon_days)).astimezone(tz).date(), tz)
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
    tasks = {t.id: t for t in session.scalars(select(StudyTaskRow))}
    for act in activity_rows:
        if act.duration_minutes is None:
            continue  # duration to confirm: never scheduled with a guessed length
        if act.all_day and not act.busy:
            continue  # informative all-day event: shown, blocks nothing
        travels = act.place_id is not None and not (act.remote or act.all_day)
        place = directory.get(act.place_id) if travels and act.place_id else None
        duration = timedelta(minutes=act.duration_minutes)
        planned = act.planned_start_at
        underway = (
            act.scheduling == "flexible"
            and planned is not None
            and planned <= now < planned + duration
        )
        fixed_start = (
            act.earliest_start_at
            if act.scheduling == "fixed"
            else planned
            if underway
            else (planned or act.preferred_start_at)
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
                    movable=act.scheduling == "flexible" and act.locked and not underway,
                    underway=underway,
                )
            )
        else:
            task = tasks.get(act.task_id) if act.task_id is not None else None
            target = (
                None
                if task is None or task.target_date is None
                else end_of_day(task.target_date, tz)
            )
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
                    task_id=None if task is None else str(task.id),
                    target=target,
                    strict=task is not None and task.target_kind is TargetKind.HARD,
                    training=act.kind == "training",
                )
            )
    expired: dict[str, int] = {}
    for act in session.scalars(
        select(ActivityRow).where(
            ActivityRow.status == "active",
            ActivityRow.task_id.is_not(None),
            ActivityRow.latest_end_at <= now,
        )
    ):
        task = tasks.get(act.task_id) if act.task_id is not None else None
        if (
            task is not None
            and not task.archived
            and task.target_kind is TargetKind.HARD
            and act.duration_minutes
        ):
            expired[str(task.id)] = act.duration_minutes

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
        target, minutes = end_of_day(target_day, tz), remaining
        if target_day > last_day:
            # Target beyond the planning horizon: plan only the proportional share now (spread
            # the work until the target); the real deadline is checked once it is in range.
            minutes = _proportional_share(remaining, today, last_day, target_day)
            target, target_kind = end_of_day(last_day, tz), TargetKind.SOFT
            spread[str(task.id)] = minutes
        demands.append(
            StudyDemand(
                str(task.id), task.subject, minutes, target, target_kind, priority=task.priority
            )
        )
    subjects = {d.task_id: d.subject for d in demands}

    locked = [
        PlannedBlock(
            str(b.task_id),
            subjects.get(str(b.task_id), "?"),
            b.start_at,
            b.end_at,
            locked=True,
            ref=f"block:{b.id}",
        )
        for b in session.scalars(
            select(StudyBlockRow).where(
                StudyBlockRow.locked.is_(True),
                StudyBlockRow.state == "planned",
                StudyBlockRow.end_at > now,
            )
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

    stays = declared_stays(session, directory)
    alternation = residence_alternation(session, directory, tz)
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
        alternation=alternation,
        preferences=Preferences(**prefs),
        buffer=buffer,
        activity_hours=activity_hours(session),
        training_preferences=TrainingPreferences(
            **(get_setting(session, "training_preferences", {}) or {})
        ),
        expired_strict_tasks=expired,
    )
    return LoadedInput(data, task_ids, spread)


def activity_hours(session: Session) -> tuple[time, time]:
    """Preferred hours of flexible activities (setting ``activity_hours``, soft)."""
    value = get_setting(session, "activity_hours")
    if not value:
        return DEFAULT_ACTIVITY_HOURS
    return time.fromisoformat(value["start"]), time.fromisoformat(value["end"])


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


def lock_block(block: StudyBlockRow) -> None:
    """Protect a study block in place: detached from plan versions, never moved without the
    user's approval (a re-plan keeps it and plans around it)."""
    block.locked = True
    block.schedule_version_id = None


def release_block(session: Session, block: StudyBlockRow) -> None:
    """Unprotect a block: it rejoins the active plan as an ordinary movable block (the next
    re-plan keeps it when nothing better exists: stability), never deleted."""
    active = active_version(session)
    block.locked = False
    block.schedule_version_id = None if active is None else active.id


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
                "returning": block.returning,
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
                "alternatives": [
                    {
                        "departure": alt.departure.isoformat(),
                        "arrival": alt.arrival.isoformat(),
                        "transfers": alt.transfers,
                        "realtime": alt.uses_realtime,
                    }
                    for alt in block.alternatives
                ],
            }
        )
    return items


LABELS = {
    CommitmentKind.CLASS: "le cours",
    CommitmentKind.RACE: "la compétition",
    CommitmentKind.TRAINING: "l'entraînement",
    CommitmentKind.OTHER: "l'activité",
}


def _publish_conflicts(session: Session, conflicts: list[Conflict], tz: ZoneInfo) -> None:
    """Publish what ``engine.find_conflicts`` detected: a proposal when approval can resolve
    it (unlock a locked block or activity), otherwise a conflict notification (rien ne bouge)."""
    for conflict in conflicts:
        other = conflict.other
        if conflict.kind is ConflictKind.LOCKED_BLOCK:
            block_id = conflict.subject_ref.removeprefix("block:")
            propose(
                session,
                "unlock_block",
                f"'{other.title}' chevauche un bloc d'étude verrouillé",
                {
                    "block_id": block_id,
                    "block_start": conflict.subject_start.isoformat(),
                    "commitment": other.ref,
                    "commitment_start": other.start.isoformat(),
                },
                f"unlock:{block_id}:{other.ref}:{other.start.isoformat()}",
            )
        elif conflict.kind is ConflictKind.LOCKED_ACTIVITY:
            assert conflict.subject_kind is not None  # noqa: S101 - activities have a kind
            propose(
                session,
                "unlock_activity",
                f"{LABELS[other.kind].capitalize()} '{other.title}' chevauche "
                f"{LABELS[conflict.subject_kind]} verrouillé(e) '{conflict.subject_title}'",
                {
                    "activity_id": conflict.subject_ref.removeprefix("activity:"),
                    "commitment": other.ref,
                    "commitment_start": other.start.isoformat(),
                },
                f"unlock_activity:{conflict.subject_ref}:{other.ref}:{other.start.isoformat()}",
            )
        else:
            assert conflict.subject_kind is not None  # noqa: S101 - commitments have a kind
            race = CommitmentKind.RACE in (conflict.subject_kind, other.kind)
            when = other.start.astimezone(tz).isoformat(timespec="minutes")
            key = (
                f"{conflict.subject_ref}:{other.ref}:{conflict.subject_start.isoformat()}:"
                f"{other.start.isoformat()}"
            )
            notify(
                session,
                "race_conflict" if race else "fixed_conflict",
                "Conflit avec une compétition" if race else "Conflit entre engagements fixes",
                f"{LABELS[conflict.subject_kind].capitalize()} '{conflict.subject_title}' "
                f"chevauche {LABELS[other.kind]} '{other.title}' ({when}). Rien n'est déplacé "
                "automatiquement : à arbitrer.",
                f"conflict:{key}",
            )


UNKNOWN_LOCATION = "unknown location '"


def _notify_issues(session: Session, issues: list[str], now: datetime, tz: ZoneInfo) -> None:
    """One notification per distinct issue; routing outages are grouped into one notice per
    day (an outage touches every journey of every re-plan: never a flood)."""
    outage = [i for i in issues if "routing unavailable" in i]
    if outage:
        day = now.astimezone(tz).date().isoformat()
        notify(
            session,
            "routing_outage",
            "Routage indisponible",
            f"Le calcul d'itinéraire est indisponible ({len(outage)} trajet(s) non calculé(s)) : "
            "le temps autour des engagements reste bloqué, rien n'est inventé. Ce n'est pas "
            "une impossibilité.",
            f"routing_outage:{day}",
        )
    for issue in issues:
        # An unknown location is asked as a question (``replan``): no second notice.
        if issue not in outage and not issue.startswith(UNKNOWN_LOCATION):
            notify(
                session, "plan_issue", "Tempo : point à vérifier", issue, f"issue:{digest(issue)}"
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
    routing = access_routing(session, routing)  # access points + realistic journeys only
    refresh_task_sessions(session, now, tz)
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
        "unplanned_minutes": outcome.unplanned_minutes,
        "late_minutes": outcome.late_minutes,
        "hard_deadline_misses": outcome.hard_deadline_misses,
        "outside_hours": outcome.outside_hours,
        "deadline_capacity": outcome.deadline_capacity,
        "conflicts": [c.describe() for c in outcome.conflicts],
    }
    usable = outcome.status in (OutcomeStatus.COMPLETE, OutcomeStatus.PARTIAL)
    previous = active_version(session)
    keep_previous = usable and bool(outcome.questions) and previous is not None
    for question in outcome.questions:  # blocking uncertainty: asked, never assumed
        notify(
            session,
            "question",
            "Tempo a besoin d'une précision",
            question
            + (
                " En attendant, le planning actuel est conservé."
                if keep_previous
                else " En attendant, ce trajet n'est pas planifié."
            ),
            f"question:{digest(question)}",
        )
    if keep_previous:  # never replace a valid plan with an incomplete one
        summary["kept_previous_plan"] = True
        usable = False
    summary["questions"] = outcome.questions
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
    elif not summary.get("kept_previous_plan"):
        reason_text = "; ".join(outcome.violations) or (
            "calcul inconclusif dans le temps imparti (ce n'est pas une impossibilité)"
        )
        notify(
            session,
            "plan_rejected",
            "Planification impossible à valider",
            f"Le nouveau planning n'a pas été appliqué (l'ancien reste actif) : {reason_text}",
            f"rejected:{digest(reason_text)}:{now.date()}",
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
    _publish_conflicts(session, outcome.conflicts, tz)
    _notify_issues(session, outcome.issues, now, tz)
    if usable:
        subjects = {str(t.id): t.subject for t in session.scalars(select(StudyTaskRow))}
        for task in outcome.hard_deadline_misses:
            minutes = outcome.unplanned_minutes.get(task, 0)
            notify(
                session,
                "deadline",
                "Échéance impossible à tenir",
                f"« {subjects.get(task, task)} » ne tient pas avant son échéance stricte "
                f"({minutes} min non planifiées).",
                f"deadline:{task}:{now.astimezone(tz).date().isoformat()}",
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
