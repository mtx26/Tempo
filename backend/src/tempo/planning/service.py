"""Database side of planning: load inputs, run the engine, persist the outcome.

Persistence rules:
- A valid outcome (``complete``, ``partial``, ``pending_approval``) becomes the new ``active``
  version; the previous one is ``superseded`` (history). ``invalid`` and ``no_solution`` never
  replace the active plan: recorded ``rejected`` and notified (a technical failure never
  destroys the last valid schedule).
- Locked blocks and activities are never moved (``unlock_*`` proposals on conflict). Existing
  placements of the current day are pinned: when one no longer fits, its move is proposed
  (``move_block`` / ``move_activity``) and its alternative slot held until the user decides;
  the same holds for an activity whose real journey no longer fits its planned slot.
- Questions, issues and proposals are published through the shared notification and proposal
  services (``db.store``), de-duplicated; a question or issue that disappeared from the latest
  plan is resolved automatically (acknowledged).
"""

import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, or_, select, text, update
from sqlalchemy.orm import Session

from tempo.const import CLASS_ATTENDANCE_SETTING, DECLINED_SETTING, EXAM_SETTING
from tempo.db.models import (
    ActivityRow,
    AvailabilityExceptionRow,
    AvailabilityRow,
    CategoryWindowRow,
    FixedEventRow,
    NotificationRow,
    ProposalRow,
    ScheduleVersion,
    StudyBlockRow,
    StudyTaskRow,
)
from tempo.db.store import (
    access_routing,
    active_version,
    attended_classes,
    audit,
    declared_stays,
    digest,
    get_setting,
    notify,
    place_directory,
    propose,
    residence_alternation,
    set_setting,
    temporary_preferences,
)
from tempo.domain.models import TargetKind, exam_status
from tempo.domain.ops import needs_session, refresh_task_sessions, remaining_minutes
from tempo.planning.engine import (
    ActivityRequest,
    Commitment,
    CommitmentKind,
    Conflict,
    ConflictKind,
    OutcomeStatus,
    PendingMove,
    PlanningInput,
    PlanningOutcome,
    run_planning,
)
from tempo.scheduler.model import (
    STUDY_PREFERENCE_KEYS,
    TRAINING_PREFERENCE_KEYS,
    PlannedBlock,
    Preferences,
    StudyDemand,
    TemporaryPreference,
    TrainingPreferences,
)
from tempo.timeutil import Interval, end_of_day, subtract, utc
from tempo.transport.places import Place
from tempo.transport.routing import Journey, Leg, RoutingProvider
from tempo.transport.travel import Previous

ACTIVITY_KINDS = {"race": CommitmentKind.RACE, "training": CommitmentKind.TRAINING}
ACTOR = "planner"
MOVE_KINDS = ("move_activity", "move_block")


class PlanningNotConfiguredError(RuntimeError):
    """A prerequisite (residence) is missing: Tempo asks instead of guessing."""


def _wall_clock(day: date, start: int, end: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    # Wall-clock minutes (DST-safe): build local times, not midnight + offset.
    begin = datetime.combine(day, time(start // 60, start % 60), tzinfo=tz)
    finish = (
        end_of_day(day, tz)
        if end == 1440
        else datetime.combine(day, time(end // 60, end % 60), tzinfo=tz)
    )
    return begin, finish


def _days(start: datetime, end: datetime, tz: ZoneInfo) -> list[date]:
    day, last = start.astimezone(tz).date(), end.astimezone(tz).date()
    out = []
    while day <= last:
        out.append(day)
        day += timedelta(days=1)
    return out


@dataclass(frozen=True, slots=True)
class Rules:
    """Availability rules of one horizon, the same mechanism for every category: weekly
    windows (study availability, category windows) widened by dated extra time and reduced by
    dated unavailability, general (all flexible activities) or of one category."""

    study: list[Interval]
    windows: dict[str, list[Interval]]
    blocked: dict[str | None, list[Interval]]

    def blocked_for(self, kind: str) -> tuple[Interval, ...]:
        return tuple(self.blocked.get(None, []) + self.blocked.get(kind, []))


def load_rules(session: Session, start: datetime, end: datetime, tz: ZoneInfo) -> Rules:
    days = _days(start, end, tz)
    exceptions = list(
        session.scalars(
            select(AvailabilityExceptionRow).where(
                AvailabilityExceptionRow.day >= days[0] - timedelta(days=1),
                AvailabilityExceptionRow.day <= days[-1],
            )
        )
    )
    weekly = list(session.scalars(select(AvailabilityRow)))
    categories = list(session.scalars(select(CategoryWindowRow)))
    study: list[Interval] = []
    windows: dict[str, list[Interval]] = {}
    blocked: dict[str | None, list[Interval]] = {}
    for day in days:
        for row in weekly:
            if row.weekday == day.weekday():
                study.append(Interval(*_wall_clock(day, row.start_minute, row.end_minute, tz)))
        for cat in categories:
            if cat.weekday == day.weekday():
                windows.setdefault(cat.kind, []).append(
                    Interval(*_wall_clock(day, cat.start_minute, cat.end_minute, tz))
                )
        for exc in (e for e in exceptions if e.day == day):
            span = Interval(*_wall_clock(day, exc.start_minute, exc.end_minute, tz))
            if not exc.available:
                blocked.setdefault(exc.kind, []).append(span)
                continue
            if exc.kind in (None, "study"):
                study.append(span)
            for kind in windows if exc.kind is None else [exc.kind]:
                if kind in windows:
                    windows[kind].append(span)
    study_blocked = blocked.get(None, []) + blocked.get("study", [])
    study = [p for w in study for p in subtract(w, study_blocked)]
    return Rules(study, windows, blocked)


@dataclass(frozen=True, slots=True)
class LoadedInput:
    data: PlanningInput
    task_ids: dict[str, uuid.UUID]
    spread: dict[str, int] = field(default_factory=dict)
    """Tasks whose deadline is beyond the horizon -> minutes planned in this horizon."""
    warnings: list[str] = field(default_factory=list)
    questions: dict[str, datetime] = field(default_factory=dict)
    """Questions about the sources (e.g. is this an exam?), with when the answer is needed."""


def _capacity(rules: Rules, busy: list[Interval], start: datetime, until: datetime) -> int:
    """Free study minutes in ``[start, until)`` (study windows minus known busy time)."""
    total = 0
    for window in rules.study:
        low, high = max(window.start, start, key=utc), min(window.end, until, key=utc)
        if utc(low) >= utc(high):
            continue
        total += sum(
            int((utc(p.end) - utc(p.start)).total_seconds() // 60)
            for p in subtract(Interval(low, high), busy)
        )
    return total


def _previous_travel(summary: dict[str, Any]) -> Previous:
    """Itineraries of the published plan by leg, reused (not revalidated) when MOTIS is down."""
    known: dict[tuple[str, str, str, bool], Journey] = {}
    for trip in summary.get("travel", []):
        if trip.get("status") not in ("routed", "stale") or not trip.get("legs"):
            continue
        legs = tuple(
            Leg(
                leg["mode"],
                leg.get("from") or "",
                leg.get("to") or "",
                datetime.fromisoformat(leg["start"]),
                datetime.fromisoformat(leg["end"]),
                datetime.fromisoformat(leg["start"]),
                datetime.fromisoformat(leg["end"]),
                bool(leg.get("realtime")),
                line=leg.get("line"),
                agency=leg.get("agency"),
            )
            for leg in trip["legs"]
        )
        journey = Journey(
            legs,
            datetime.fromisoformat(trip["departure"]),
            datetime.fromisoformat(trip["arrival"]),
            max(0, sum(1 for leg in legs if leg.mode not in ("WALK", "ACCESS")) - 1),
            "previous plan",
        )
        outbound = not trip.get("returning")
        anchor = trip["end"] if outbound else trip["start"]
        key = (
            trip["origin"],
            trip["destination"],
            utc(datetime.fromisoformat(anchor)).isoformat(),
            outbound,
        )
        known[key] = journey

    def lookup(origin: Place, destination: Place, at: datetime, outbound: bool) -> Journey | None:
        return known.get((origin.id, destination.id, utc(at).isoformat(), outbound))

    return lookup


def build_input(
    session: Session, now: datetime, tz: ZoneInfo, *, allow_today: bool = False
) -> LoadedInput:
    directory = place_directory(session)
    alternation = residence_alternation(session, directory, tz, now)
    if alternation is None:
        raise PlanningNotConfiguredError(
            "no residence configured (say where you live: set_residence_alternation)"
        )
    horizon_days = int(get_setting(session, "horizon_days"))
    horizon_end = end_of_day((now + timedelta(days=horizon_days)).astimezone(tz).date(), tz)
    today = now.astimezone(tz).date()
    tomorrow = datetime.combine(today + timedelta(days=1), time(0), tzinfo=tz)
    rules = load_rules(session, now, horizon_end, tz)
    attendance = get_setting(session, CLASS_ATTENDANCE_SETTING, {}) or {}
    declined = {
        ref
        for ref, day in (get_setting(session, DECLINED_SETTING, {}) or {}).items()
        if day == today.isoformat()
    }
    pending = list(
        session.scalars(
            select(ProposalRow).where(
                ProposalRow.status == "pending", ProposalRow.kind.in_(MOVE_KINDS)
            )
        )
    )
    held_refs = {p.details["ref"] for p in pending}
    reserved = [
        Interval(
            datetime.fromisoformat(p.details["to_start"]),
            datetime.fromisoformat(p.details["to_end"]),
        )
        for p in pending
        if p.details.get("to_start")
    ]
    held_minutes: dict[str, int] = {}
    for p in pending:
        if p.kind == "move_block" and p.details.get("task_id"):
            task = p.details["task_id"]
            held_minutes[task] = held_minutes.get(task, 0) + int(p.details.get("minutes", 0))

    commitments: list[Commitment] = []
    answers = get_setting(session, EXAM_SETTING, {}) or {}
    uids = {
        f"event:{row.id}": row.uid
        for row in session.scalars(
            select(FixedEventRow).where(
                FixedEventRow.end_at > now, FixedEventRow.start_at < horizon_end
            )
        )
    }
    questions: dict[str, datetime] = {}
    for ref, title, start, end, location in attended_classes(session, now, horizon_end):
        exam = answers.get(uids.get(ref, ""), exam_status(title))
        if exam is None:  # uncertain: asked once per series (the answer covers all of it);
            # meanwhile the full arrival margin (safe side). Stable text: never re-notified.
            question = (
                f"« {title} » est-il un examen ? (réponse valable pour toutes ses séances ; en "
                "attendant, je garde toute la marge d'arrivée)"
            )
            questions[question] = min(questions.get(question, start), start, key=utc)
        commitments.append(
            Commitment(
                ref,
                title,
                start,
                end,
                CommitmentKind.CLASS if exam is False else CommitmentKind.EXAM,
                directory.resolve(location),
                location,
                exact_times=ref in attendance,
            )
        )

    requests: list[ActivityRequest] = []
    rows = list(
        session.scalars(
            select(ActivityRow).where(
                ActivityRow.status == "active",
                ActivityRow.latest_end_at > now,
                ActivityRow.earliest_start_at < horizon_end,
            )
        )
    )
    tasks = {t.id: t for t in session.scalars(select(StudyTaskRow))}
    for act in rows:
        ref = f"activity:{act.id}"
        if act.duration_minutes is None or ref in held_refs:
            continue  # duration to confirm, or a move waiting for the user's decision
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
        pinned = (
            act.scheduling == "flexible"
            and not act.locked
            and not underway
            and not allow_today
            and planned is not None
            and planned > now
            and planned.astimezone(tz).date() == today
        )
        fixed_start = (
            act.earliest_start_at
            if act.scheduling == "fixed"
            else planned
            if underway or pinned
            else (planned or act.preferred_start_at)
            if act.locked
            else None
        )
        task = tasks.get(act.task_id) if act.task_id is not None else None
        if fixed_start is not None:
            commitments.append(
                Commitment(
                    ref,
                    act.title,
                    fixed_start,
                    fixed_start + duration,
                    ACTIVITY_KINDS.get(act.kind, CommitmentKind.OTHER),
                    place,
                    movable=act.scheduling == "flexible" and act.locked and not underway,
                    underway=underway,
                    pinned=pinned,
                    at_home=act.kind == "training" and place is None,
                )
            )
            if not pinned:
                continue  # a pinned item also gets a request: its alternative when displaced
        earliest = act.earliest_start_at
        if ref in declined:
            earliest = max(earliest, tomorrow, key=utc)
            if utc(earliest) >= utc(act.latest_end_at):
                continue
        requests.append(
            ActivityRequest(
                str(act.id),
                act.title,
                duration,
                earliest,
                act.latest_end_at,
                place=place,
                preferred_start=act.preferred_start_at,
                previous_start=act.planned_start_at,
                preferred_day=act.preferred_day,
                sequence_group=act.sequence_group,
                sequence_index=act.sequence_index or 0,
                rest_days=act.rest_days,
                allowed=None if act.kind not in rules.windows else tuple(rules.windows[act.kind]),
                blocked=rules.blocked_for(act.kind),
                task_id=None if task is None else str(task.id),
                deadline=None
                if task is None or task.target_date is None
                else end_of_day(task.target_date, tz),
                strict=task is not None and task.target_kind is TargetKind.HARD,
                training=act.kind == "training",
                outdoor=not act.indoor,
                priority=act.priority,
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
            expired[str(task.id)] = expired.get(str(task.id), 0) + act.duration_minutes

    # Study demands: full remaining work within the horizon. A deadline beyond it plans its
    # share now: proportional to the study capacity of the horizon, and at least what the
    # known capacity after the horizon cannot absorb, deadlines taken in order (earliest
    # deadline first, with the classes and fixed events known until then).
    task_ids: dict[str, uuid.UUID] = {}
    demands: list[StudyDemand] = []
    spread: dict[str, int] = {}
    warnings: list[str] = []
    last_day = (horizon_end - timedelta(minutes=1)).astimezone(tz).date()
    open_tasks = [
        t
        for t in session.scalars(select(StudyTaskRow).where(StudyTaskRow.archived.is_(False)))
        if remaining_minutes(t) and not needs_session(t)
    ]
    far = sorted(
        (t for t in open_tasks if t.target_date is not None and t.target_date > last_day),
        key=lambda t: t.target_date or date.max,
    )
    if far:
        last = end_of_day(max(t.target_date or last_day for t in far), tz)
        far_rules = load_rules(session, now, last, tz)
        known = [Interval(c[2], c[3]) for c in attended_classes(session, now, last)] + [
            Interval(a.earliest_start_at, a.latest_end_at)
            for a in session.scalars(
                select(ActivityRow).where(
                    ActivityRow.status == "active",
                    ActivityRow.scheduling == "fixed",
                    ActivityRow.latest_end_at > now,
                    ActivityRow.earliest_start_at < last,
                )
            )
        ]
        within = _capacity(far_rules, known, now, horizon_end)
        needed, planned_now = 0, 0
        for task in far:
            remaining = max(0, (remaining_minutes(task) or 0) - held_minutes.get(str(task.id), 0))
            deadline = end_of_day(task.target_date or last_day, tz)
            total = _capacity(far_rules, known, now, deadline)
            needed += remaining
            if needed > total:
                warnings.append(
                    f"« {task.subject} » : avec les échéances plus proches, {needed} min à "
                    f"faire pour {total} min d'étude disponibles d'ici le "
                    f"{(task.target_date or last_day).isoformat()} : la répartition régulière "
                    "ne suffit plus, à revoir avec l'utilisateur"
                )
            proportional = remaining if total <= 0 else remaining * within / total
            unabsorbed = needed - _capacity(far_rules, known, horizon_end, deadline) - planned_now
            share = math.ceil(max(proportional, unabsorbed, 15) / 15) * 15
            spread[str(task.id)] = min(remaining, share)
            planned_now += spread[str(task.id)]
    for task in open_tasks:
        remaining = max(0, (remaining_minutes(task) or 0) - held_minutes.get(str(task.id), 0))
        task_ids[str(task.id)] = task.id
        target: datetime | None = None
        minutes = remaining
        if str(task.id) in spread:
            target, minutes = horizon_end, spread[str(task.id)]
        elif task.target_date is not None:
            target = end_of_day(task.target_date, tz)
        not_before = tomorrow if f"task:{task.id}" in declined else None
        demands.append(
            StudyDemand(
                str(task.id),
                task.subject,
                minutes,
                target,
                task.target_kind,
                priority=task.priority,
                not_before=not_before,
            )
        )
    subjects = {d.task_id: d.subject for d in demands}

    def blocks_of(rows_: Any, *, locked: bool) -> list[PlannedBlock]:
        return [
            PlannedBlock(
                str(b.task_id),
                subjects[str(b.task_id)],
                b.start_at,
                b.end_at,
                locked=locked,
                ref=f"block:{b.id}",
            )
            for b in rows_
            if str(b.task_id) in subjects
        ]

    locked_blocks = blocks_of(
        session.scalars(
            select(StudyBlockRow).where(
                StudyBlockRow.locked.is_(True),
                StudyBlockRow.state == "planned",
                StudyBlockRow.end_at > now,
            )
        ),
        locked=True,
    )
    active = active_version(session)
    current: list[PlannedBlock] = []
    if active is not None:
        current = blocks_of(
            session.scalars(
                select(StudyBlockRow).where(
                    StudyBlockRow.schedule_version_id == active.id,
                    StudyBlockRow.locked.is_(False),
                    StudyBlockRow.state == "planned",
                    StudyBlockRow.end_at > now,
                )
            ),
            locked=False,
        )
    underway_blocks = [
        replace(b, locked=True, ref=None) for b in current if utc(b.start) <= utc(now)
    ]
    pinned_blocks = [
        b
        for b in current
        if utc(b.start) > utc(now) and b.start.astimezone(tz).date() == today and not allow_today
    ]
    previous = [b for b in current if utc(b.start) > utc(now) and b not in pinned_blocks]

    study_prefs = {
        k: v
        for k, v in (get_setting(session, "study_preferences", {}) or {}).items()
        if k in STUDY_PREFERENCE_KEYS
    }
    training = _training(get_setting(session, "training_preferences", {}) or {})
    temporary = [
        TemporaryPreference(
            datetime.fromisoformat(item["start"]),
            datetime.fromisoformat(item["end"]),
            training=_training(
                {**(get_setting(session, "training_preferences", {}) or {}), **item["value"]}
            )
            if item["key"] == "training_preferences"
            else None,
            activity_hours=_hours(item["value"]) if item["key"] == "activity_hours" else None,
        )
        for item in temporary_preferences(session, now)
    ]
    data = PlanningInput(
        now=now,
        horizon_end=horizon_end,
        timezone=tz,
        alternation=alternation,
        commitments=commitments,
        activities=requests,
        availability=rules.study,
        demands=demands,
        fixed_blocks=locked_blocks + underway_blocks,
        pinned_blocks=pinned_blocks,
        previous_blocks=previous,
        stays=declared_stays(session, directory),
        reserved=reserved,
        preferences=Preferences(**study_prefs),
        training_preferences=training,
        temporary=temporary,
        activity_hours=_hours(get_setting(session, "activity_hours")),
        buffer=timedelta(minutes=int(get_setting(session, "buffer_minutes"))),
        class_tolerance=timedelta(minutes=int(get_setting(session, "class_tolerance_minutes"))),
        max_journey=timedelta(minutes=int(get_setting(session, "max_journey_minutes"))),
        previous_travel=_previous_travel(active.summary) if active is not None else None,
        expired_strict_tasks=expired,
    )
    return LoadedInput(data, task_ids, spread, warnings, questions)


def _training(raw: dict[str, Any]) -> TrainingPreferences:
    return TrainingPreferences(**{k: v for k, v in raw.items() if k in TRAINING_PREFERENCE_KEYS})


def _hours(value: dict[str, str]) -> tuple[time, time]:
    return time.fromisoformat(value["start"]), time.fromisoformat(value["end"])


def lock_block(block: StudyBlockRow) -> None:
    """Protect a study block in place (explicit request only): detached from plan versions,
    never moved without the user's approval."""
    block.locked = True
    block.schedule_version_id = None


def release_block(session: Session, block: StudyBlockRow) -> None:
    """Unprotect a block: it rejoins the active plan as an ordinary block, never deleted."""
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
                "event_title": block.event_title,
                "event_start": None if block.event_start is None else block.event_start.isoformat(),
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
    CommitmentKind.EXAM: "l'examen",
    CommitmentKind.RACE: "la compétition",
    CommitmentKind.TRAINING: "l'entraînement",
    CommitmentKind.OTHER: "l'activité",
}


def _publish_conflicts(session: Session, conflicts: list[Conflict], tz: ZoneInfo) -> set[str]:
    """Proposals when approval can resolve a conflict (unlock), else a notification."""
    keys: set[str] = set()
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
                f"conflict:{conflict.subject_ref}:{other.ref}:{conflict.subject_start.isoformat()}:"
                f"{other.start.isoformat()}"
            )
            keys.add(key[:300])
            notify(
                session,
                "race_conflict" if race else "fixed_conflict",
                "Conflit avec une compétition" if race else "Conflit entre engagements fixes",
                f"{LABELS[conflict.subject_kind].capitalize()} '{conflict.subject_title}' "
                f"chevauche {LABELS[other.kind]} '{other.title}' ({when}). Rien n'est déplacé "
                "automatiquement : à arbitrer.",
                key,
            )
    return keys


def _publish_moves(session: Session, pending: list[PendingMove], tz: ZoneInfo) -> None:
    """One proposal per move waiting for approval; its target slot is held meanwhile."""
    for move in pending:
        when = move.start.astimezone(tz).strftime("%d/%m %H:%M")
        target = (
            "aucun autre créneau trouvé : la retirer de la journée ?"
            if move.to_start is None
            else f"proposition : {move.to_start.astimezone(tz).strftime('%d/%m %H:%M')}"
        )
        why = (
            "le trajet réel ne permet plus d'y arriver"
            if move.reason == "itinerary"
            else "elle ne tient plus dans la journée"
        )
        kind = "move_block" if move.ref.startswith("block") else "move_activity"
        details: dict[str, Any] = {
            "ref": move.ref,
            "task_id": move.task_id,
            "title": move.title,
            "from_start": move.start.isoformat(),
            "from_end": move.end.isoformat(),
            "to_start": None if move.to_start is None else move.to_start.isoformat(),
            "to_end": None if move.to_end is None else move.to_end.isoformat(),
            "minutes": int((utc(move.end) - utc(move.start)).total_seconds() // 60),
            "reason": move.reason,
        }
        if kind == "move_activity":
            details["activity_id"] = move.ref.removeprefix("activity:")
        propose(
            session,
            kind,
            f"Déplacer « {move.title} » ({when}) : {why} ; {target}",
            details,
            f"move:{move.ref}:{move.start.isoformat()}",
        )


def _expire_moves(session: Session, now: datetime) -> None:
    """A move proposal whose slot has passed can no longer be applied."""
    for row in session.scalars(
        select(ProposalRow).where(ProposalRow.status == "pending", ProposalRow.kind.in_(MOVE_KINDS))
    ):
        moment = row.details.get("to_start") or row.details["from_start"]
        if datetime.fromisoformat(moment) <= now:
            row.status, row.decided_at = "obsolete", now


UNKNOWN_LOCATION = "unknown location '"


def _publish_issues(
    session: Session, outcome: PlanningOutcome, now: datetime, tz: ZoneInfo, keep: set[str]
) -> None:
    """Questions and issues through the shared notification service; those that are no longer
    raised by the latest plan are resolved (acknowledged): one unresolved-issue tracking."""
    raised = set(keep)
    for question in outcome.questions:
        key = f"question:{digest(question)}"
        raised.add(key)
        notify(
            session,
            "question",
            "Tempo a besoin d'une précision",
            question,
            key,
            due_at=outcome.question_due.get(question),
        )
    outage = [i for i in outcome.issues if "routing unavailable" in i]
    if outage:
        key = f"routing_outage:{now.astimezone(tz).date().isoformat()}"
        raised.add(key)
        notify(
            session,
            "routing_outage",
            "Routage indisponible",
            f"Le calcul d'itinéraire est indisponible ({len(outage)} trajet(s) non revalidé(s)) : "
            "les itinéraires déjà calculés sont gardés, le reste du temps autour des engagements "
            "est bloqué, rien n'est inventé. Ce n'est pas une impossibilité ; recalcul "
            "automatique dès le retour du service.",
            key,
        )
    for issue in outcome.issues:
        if issue in outage or issue.startswith(UNKNOWN_LOCATION):
            continue  # grouped above / asked as a question
        key = f"issue:{digest(issue)}"
        raised.add(key)
        notify(session, "plan_issue", "Tempo : point à vérifier", issue, key)
    # Points the latest plan no longer raises are resolved (questions asked in the
    # conversation, keyed "ask:", are resolved by the conversation itself).
    for row in session.scalars(
        select(NotificationRow).where(
            NotificationRow.resolved_at.is_(None),
            NotificationRow.kind.in_(("question", "plan_issue", "routing_outage")),
            ~NotificationRow.dedupe_key.startswith("ask:"),
        )
    ):
        if row.dedupe_key not in raised:
            row.resolved_at = now
            row.acknowledged_at = row.acknowledged_at or now


def mark_missed_blocks(session: Session, now: datetime) -> int:
    """Past blocks of the current plan never reported done become ``missed`` and are notified
    once; their effort is planned again. Cancelled blocks and blocks of completed tasks are
    never reported as missed."""
    active = active_version(session)
    current: ColumnElement[bool] = StudyBlockRow.locked.is_(True)
    if active is not None:
        current = or_(current, StudyBlockRow.schedule_version_id == active.id)
    open_tasks = {
        t.id: t.subject
        for t in session.scalars(select(StudyTaskRow).where(StudyTaskRow.archived.is_(False)))
    }
    rows = [
        r
        for r in session.scalars(
            select(StudyBlockRow).where(
                StudyBlockRow.state == "planned", StudyBlockRow.end_at <= now, current
            )
        )
        if r.task_id in open_tasks
    ]
    for row in rows:
        row.state = "missed"
        notify(
            session,
            "missed_block",
            "Session d'étude manquée",
            f"La session « {open_tasks[row.task_id]} » du "
            f"{row.start_at.isoformat(timespec='minutes')} n'a pas été marquée comme faite : "
            "son temps est replanifié. Marquez-la comme faite si vous l'avez réalisée.",
            f"missed:{row.id}",
        )
    return len(rows)


REPLAN_LOCK = 0x7E4D0
"""PostgreSQL advisory lock: the API and the worker never re-plan concurrently."""


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
    before: set[tuple[str, str, str]], after: set[tuple[str, str, str]], moved_activities: list[str]
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
    allow_today: bool = False,
) -> ScheduleVersion:
    """Compute and publish a new plan.

    ``mode = "global"`` (explicit request only) drops stability; ``allow_today`` (explicit
    request to reorganise the current day, implied by global) lets today's placements move
    without a proposal. Locked and under-way items stay protected in every mode."""
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": REPLAN_LOCK})
    routing = access_routing(session, routing)  # access points + realistic journeys only
    refresh_task_sessions(session, now, tz)
    _expire_moves(session, now)
    missed = mark_missed_blocks(session, now)
    loaded = build_input(session, now, tz, allow_today=allow_today or mode == "global")
    if mode == "global":
        loaded = replace(
            loaded,
            data=replace(
                loaded.data,
                previous_blocks=[],
                activities=[replace(t, previous_start=None) for t in loaded.data.activities],
            ),
        )
    before_blocks = _block_keys(session, active_version(session))
    before_activities = {t.activity_id: t.previous_start for t in loaded.data.activities}
    outcome = run_planning(loaded.data, routing)
    for question, due in loaded.questions.items():
        outcome.questions.append(question)
        outcome.question_due[question] = due
    if loaded.questions and outcome.status in (
        OutcomeStatus.COMPLETE,
        OutcomeStatus.PENDING_APPROVAL,
    ):
        outcome.status = OutcomeStatus.PARTIAL  # an open question is never a complete plan
    result = outcome.result
    spread = loaded.spread
    # A share planned for a far deadline is not a missed deadline: reported as a warning.
    misses = [t for t in outcome.deadline_misses if t not in spread]
    behind = [t for t in outcome.deadline_misses if t in spread]
    summary: dict[str, Any] = {
        "missed_blocks": missed,
        "spread": spread,
        "reason": reason,
        "mode": mode,
        "planned_at": now.isoformat(),
        "horizon_end": loaded.data.horizon_end.isoformat(),
        "status": outcome.status.value,
        "issues": outcome.issues,
        "warnings": outcome.warnings + loaded.warnings,
        "violations": outcome.violations,
        "unplaced_activities": outcome.unplaced_activities,
        "travel": _travel_json(outcome),
        "unplanned_minutes": outcome.unplanned_minutes,
        "hard_deadline_misses": [t for t in outcome.hard_deadline_misses if t not in spread],
        "deadline_misses": misses,
        "behind_regular_pace": behind,
        "outside_hours": outcome.outside_hours,
        "deadline_capacity": {k: v for k, v in outcome.deadline_capacity.items() if k in misses},
        "conflicts": [c.describe() for c in outcome.conflicts],
        "questions": outcome.questions,
        "pending_approval": [
            {
                "ref": p.ref,
                "title": p.title,
                "from": p.start.isoformat(),
                "to": None if p.to_start is None else p.to_start.isoformat(),
            }
            for p in outcome.pending
        ],
        "unconfirmed_journeys": outcome.unconfirmed_journeys,
        "journeys_to_refresh": sum(
            1 for b in outcome.travel if b.status.value in ("stale", "unknown")
        ),
    }
    usable = outcome.status in (
        OutcomeStatus.COMPLETE,
        OutcomeStatus.PARTIAL,
        OutcomeStatus.PENDING_APPROVAL,
    )
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
        stored_locked = {b.ref for b in loaded.data.fixed_blocks if b.ref is not None}
        for block in result.blocks:
            if block.ref is not None and block.ref in stored_locked:
                continue  # user-locked rows stay as they are, outside versions
            session.add(
                StudyBlockRow(
                    task_id=loaded.task_ids[block.task_id],
                    schedule_version_id=version.id,
                    start_at=block.start,
                    end_at=block.end,
                )
            )
        for placed in outcome.activities:
            session.execute(
                update(ActivityRow)
                .where(ActivityRow.id == uuid.UUID(placed.activity_id))
                .values(planned_start_at=placed.start)
            )
        for missing in outcome.unplaced_activities:
            session.execute(
                update(ActivityRow)
                .where(ActivityRow.id == uuid.UUID(missing))
                .values(planned_start_at=None)
            )
        _publish_moves(session, outcome.pending, tz)
    else:
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
            for placed in outcome.activities
            if (before := before_activities.get(placed.activity_id)) is not None
            and utc(before) != utc(placed.start)
        ]
        summary["diff"] = _schedule_diff(before_blocks, _block_keys(session, version), moved)
        version.summary = dict(summary)
    keep = _publish_conflicts(session, outcome.conflicts, tz)
    if usable:
        _publish_issues(session, outcome, now, tz, keep)
        subjects = {str(t.id): t.subject for t in session.scalars(select(StudyTaskRow))}
        for task in misses:
            minutes = outcome.unplanned_minutes.get(task, 0)
            strict = task in outcome.hard_deadline_misses
            notify(
                session,
                "deadline",
                "Échéance impossible à tenir" + (" (stricte)" if strict else ""),
                f"« {subjects.get(task, task)} » : {minutes} min ne tiennent pas avant "
                "l'échéance. Le réalisable est planifié ; rien n'est placé après l'échéance sans "
                "votre accord : demandez les alternatives à Tempo (reporter, réduire...).",
                f"deadline:{task}:{now.astimezone(tz).date().isoformat()}",
            )
        for warning in loaded.warnings:
            notify(
                session,
                "plan_issue",
                "Tempo : charge à surveiller",
                warning,
                f"pace:{digest(warning)}",
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


def decline_today(session: Session, ref: str, now: datetime, tz: ZoneInfo) -> None:
    """The user declined a same-day move: the item is not placed again today."""
    today = now.astimezone(tz).date().isoformat()
    current = {
        k: v for k, v in (get_setting(session, DECLINED_SETTING, {}) or {}).items() if v == today
    }
    current[ref] = today
    set_setting(session, DECLINED_SETTING, current)


__all__ = [
    "LoadedInput",
    "PlanningNotConfiguredError",
    "build_input",
    "decline_today",
    "load_rules",
    "lock_block",
    "mark_missed_blocks",
    "release_block",
    "replan",
]
