"""Deterministic planning pipeline: fixed events -> flexible activities -> travel -> study.

Pure function over plain inputs (no database, no clock), so it can be tested exhaustively and
re-run by the worker. Rules (ARCHITECTURE.md §7-8):

1. University events, races, fixed, locked and under-way activities are fixed. An event whose
   location text is not a known place/alias still blocks its time but gets no travel:
   reported, never guessed.
2. Journeys between fixed commitments are routed first, so flexible activities are placed
   around commitments AND the travel they require (``scheduler.activities``). The travel of a
   flexible activity is reserved around it from a real journey estimate; when none is known
   nothing is invented: no reservation, and the problem is reported.
3. An outing starts from the base at its start and returns to the base at its end
   (``location.base_at``: planned location covering the instant, else home): the base may
   change during a day. There is no automatic switching between family homes.
4. Mandatory travel between located commitments is routed (``transport.travel``); impossible
   or unknown journeys block the gap and are reported. A day is split into outings: the user
   goes back to the base between two commitments only when at least ``MIN_TIME_AT_BASE``
   remains there after both real journeys; otherwise the journey goes directly.
5. Study availability windows (with their place) are reduced by travel to/from commitments.
6. CP-SAT places study; the independent validator checks the WHOLE plan (study and every
   other item); with violations nothing is published (``status = invalid``).

Status: ``complete`` only when all work, every activity and every journey is placed;
``partial`` when something is left (reported); ``no_solution`` when a solver gave no answer in
its time limit (inconclusive, the previous plan stays); ``invalid`` on violations.
"""

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from tempo.scheduler.activities import (
    FlexibleActivity,
    PlacedActivity,
    TrainingPreferences,
    plan_activities,
)
from tempo.scheduler.model import (
    PlannedBlock,
    PlanningProblem,
    PlanResult,
    PlanStatus,
    Preferences,
    StudyDemand,
)
from tempo.scheduler.solver import solve
from tempo.scheduler.validate import Occupied, PlacedSession, validate_plan, validate_schedule
from tempo.timeutil import Interval, utc
from tempo.transport.itinerary import CachedRouting, Objective, TripQuery, find_trips
from tempo.transport.location import Alternation, DeclaredStay, LocatedEvent, base_at
from tempo.transport.places import Place
from tempo.transport.routing import RoutingProvider, RoutingUnavailableError
from tempo.transport.travel import (
    DEFAULT_BUFFER,
    MAX_TRAVEL,
    StudyWindow,
    TravelBlock,
    TravelStatus,
    plan_day_travel,
    subtract,
    travel_aware_study_windows,
)

MIN_TIME_AT_BASE = timedelta(hours=1)
"""Between two commitments further apart than this, the user goes back to the base."""
DEFAULT_ACTIVITY_HOURS = (time(6, 0), time(22, 0))
"""Preferred hours of flexible activities (soft; setting ``activity_hours``)."""


class CommitmentKind(StrEnum):
    CLASS = "class"
    RACE = "race"
    TRAINING = "training"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Commitment:
    """A fixed commitment (iCal class, race, fixed or locked activity, activity under way)."""

    ref: str
    title: str
    start: datetime
    end: datetime
    kind: CommitmentKind
    place: Place | None = None
    location_text: str | None = None
    movable: bool = False
    """A LOCKED flexible activity: it may move again, but only after the user's approval."""
    underway: bool = False
    """Already started: never moved, never the subject of a conflict proposal."""
    exact_times: bool = False
    """Times stated by the user for this occurrence (partial attendance): no tolerance."""


@dataclass(frozen=True, slots=True)
class TrainingRequest:
    """A flexible activity to place (training, personal outing, located task session)."""

    activity_id: str
    title: str
    duration: timedelta
    earliest_start: datetime
    latest_end: datetime
    place: Place | None = None
    preferred_start: datetime | None = None
    previous_start: datetime | None = None
    preferred_day: date | None = None
    sequence_group: str | None = None
    sequence_index: int = 0
    min_days_after_previous: int = 0
    allowed: tuple[Interval, ...] | None = None
    """Category windows (B07): the session must lie inside one of them."""
    task_id: str | None = None
    """Located / non-splittable task this session belongs to."""
    target: datetime | None = None
    """Task target (end of the target day); strict when ``strict``."""
    strict: bool = False
    training: bool = False
    """A training session (the user's training preferences apply)."""


@dataclass(frozen=True, slots=True)
class PlanningInput:
    now: datetime
    horizon_end: datetime
    timezone: ZoneInfo
    home: Place
    commitments: list[Commitment]
    trainings: list[TrainingRequest]
    availability: list[StudyWindow]
    demands: list[StudyDemand]
    locked_blocks: list[PlannedBlock] = field(default_factory=list)
    previous_blocks: list[PlannedBlock] = field(default_factory=list)
    stays: list[DeclaredStay] = field(default_factory=list)
    alternation: Alternation | None = None
    """Recurring residence schedule; ``stays`` are exceptions to it."""
    preferences: Preferences = field(default_factory=Preferences)
    buffer: timedelta = DEFAULT_BUFFER
    class_tolerance: timedelta = timedelta(0)
    """The user may arrive this late at a class and leave it this early (journeys only)."""
    solver_seconds: float = 10.0
    activity_hours: tuple[time, time] = DEFAULT_ACTIVITY_HOURS
    training_preferences: TrainingPreferences = field(default_factory=TrainingPreferences)
    expired_strict_tasks: dict[str, int] = field(default_factory=dict)
    """Task sessions whose strict deadline already passed with work left: task -> minutes."""


def _daylight(place: Place) -> Callable[[date], tuple[datetime, datetime]]:
    """Sunrise and sunset at ``place`` (astral; Belgium never has polar days)."""
    from astral import Observer
    from astral.sun import sun

    observer = Observer(place.coordinates.lat, place.coordinates.lon)

    @functools.cache
    def at(day: date) -> tuple[datetime, datetime]:
        times = sun(observer, date=day, tzinfo=UTC)
        return times["sunrise"], times["sunset"]

    return at


class ConflictKind(StrEnum):
    LOCKED_BLOCK = "locked_block"
    """A commitment overlaps a locked study block: unlocking it needs approval."""
    LOCKED_ACTIVITY = "locked_activity"
    """A commitment overlaps a locked flexible activity: unlocking it needs approval."""
    FIXED = "fixed"
    """Two fixed commitments overlap: nothing can move automatically, to arbitrate."""


@dataclass(frozen=True, slots=True)
class Conflict:
    """One overlap between protected items. ``subject`` is what may move after the user's
    approval (a locked block or activity); for FIXED it is simply the earlier commitment."""

    kind: ConflictKind
    subject_ref: str
    subject_title: str
    subject_start: datetime
    subject_kind: CommitmentKind | None
    """None for a study block."""
    other: Commitment

    def describe(self) -> str:
        what = "study block" if self.subject_kind is None else self.subject_kind.value
        return (
            f"{self.kind.value}: {what} '{self.subject_title}' "
            f"({self.subject_start.isoformat(timespec='minutes')}) overlaps "
            f"{self.other.kind.value} '{self.other.title}' "
            f"({self.other.start.isoformat(timespec='minutes')})"
        )


def find_conflicts(
    commitments: list[Commitment], locked_blocks: list[PlannedBlock], now: datetime
) -> list[Conflict]:
    """Every overlap between protected items, whatever their category: commitment pairs
    (classes, races, fixed appointments, locked or under-way activities) and stored locked
    study blocks against commitments. Under-way items are never the subject of a proposal.
    Detection only: publication (proposals, notifications) is ``planning.service``'s job, and
    a conflict without possible resolution is still reported (FIXED)."""
    found: list[Conflict] = []
    items = sorted((c for c in commitments if utc(c.end) > utc(now)), key=lambda c: utc(c.start))
    for i, first in enumerate(items):
        for second in items[i + 1 :]:
            if utc(second.start) >= utc(first.end):
                break
            movable = [c for c in (first, second) if c.movable and not c.underway]
            for subject in movable:
                other = second if subject is first else first
                found.append(
                    Conflict(
                        ConflictKind.LOCKED_ACTIVITY,
                        subject.ref,
                        subject.title,
                        subject.start,
                        subject.kind,
                        other,
                    )
                )
            if not movable:
                found.append(
                    Conflict(
                        ConflictKind.FIXED, first.ref, first.title, first.start, first.kind, second
                    )
                )
    for block in locked_blocks:
        if block.ref is None or utc(block.end) <= utc(now):
            continue  # computed / under-way blocks are not stored protections
        for commitment in items:
            if block.interval.overlaps(Interval(commitment.start, commitment.end)):
                found.append(
                    Conflict(
                        ConflictKind.LOCKED_BLOCK,
                        block.ref,
                        block.subject,
                        block.start,
                        None,
                        commitment,
                    )
                )
    return found


class OutcomeStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NO_SOLUTION = "no_solution"
    INVALID = "invalid"
    """The validator rejected the plan: nothing may be published."""


@dataclass(slots=True)
class PlanningOutcome:
    status: OutcomeStatus
    result: PlanResult | None
    trainings: list[PlacedActivity] = field(default_factory=list)
    unplaced_trainings: list[str] = field(default_factory=list)
    travel: list[TravelBlock] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    deadline_capacity: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Per missed strict deadline: work needed vs free study time before it (B12 diagnosis)."""
    unplanned_minutes: dict[str, int] = field(default_factory=dict)
    """Every task (study blocks and task sessions) -> minutes left unplanned."""
    late_minutes: dict[str, int] = field(default_factory=dict)
    hard_deadline_misses: list[str] = field(default_factory=list)
    outside_hours: list[str] = field(default_factory=list)
    """Activities placed outside the preferred activity hours (soft preference not met)."""
    conflicts: list[Conflict] = field(default_factory=list)
    """Overlaps between protected items (``find_conflicts``), reported in every outcome."""
    questions: list[str] = field(default_factory=list)
    """Blocking uncertainties to ask the user (e.g. where a class takes place): a valid plan
    already published is kept until they are answered (``planning.service.replan``)."""


def free_minutes(availability: list[Interval], busy: list[Interval], until: datetime) -> int:
    """Study-window minutes before ``until`` not taken by busy time (commitments, travel,
    trainings, locked blocks). Shared by every task: an upper bound for one task."""
    total = 0.0
    for window in availability:
        start, end = utc(window.start), min(utc(window.end), utc(until))
        if end <= start:
            continue
        cuts = sorted(
            (max(start, utc(b.start)), min(end, utc(b.end)))
            for b in busy
            if utc(b.start) < end and utc(b.end) > start
        )
        covered, cursor = 0.0, start
        for c_start, c_end in cuts:
            c_start = max(c_start, cursor)
            if c_end > c_start:
                covered += (c_end - c_start).total_seconds()
                cursor = c_end
        total += (end - start).total_seconds() - covered
    return int(total // 60)


def _activity_travel(
    request: TrainingRequest, base: Place | None, routing: RoutingProvider, issues: list[str]
) -> tuple[timedelta, timedelta]:
    """Travel time to reserve around a flexible activity before it is placed: real journeys
    around its preferred time. Unknown: nothing is invented (zero, reported); the journeys
    actually needed are routed once it is placed and block their gap when they fail."""
    if request.place is None or request.place == base:
        return timedelta(0), timedelta(0)
    if base is None:
        issues.append(f"activity '{request.title}': travel origin unknown; travel not reserved")
        return timedelta(0), timedelta(0)
    anchor = request.preferred_start or request.earliest_start
    try:
        there = find_trips(
            routing,
            TripQuery(
                base.coordinates, request.place.coordinates, arrive_before=anchor, span=MAX_TRAVEL
            ),
        ).best
        back = find_trips(
            routing,
            TripQuery(
                request.place.coordinates,
                base.coordinates,
                depart_after=anchor + request.duration,
                span=MAX_TRAVEL,
            ),
        ).best
    except RoutingUnavailableError as exc:
        issues.append(
            f"activity '{request.title}': routing unavailable ({exc}); travel not reserved"
        )
        return timedelta(0), timedelta(0)
    if there is None or back is None:
        issues.append(
            f"activity '{request.title}': no journey to/from {request.place.name}; "
            "travel not reserved"
        )
    return (
        timedelta(0) if there is None else there.duration,
        timedelta(0) if back is None else back.duration,
    )


def _away_spans(
    events: list[LocatedEvent], travel: list[TravelBlock], tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    """Per local day, from the first departure to the last return of the outings to fixed
    commitments (unknown or impossible returns included, as routed conservatively): the user
    is not at the base then."""
    spans: dict[date, tuple[datetime, datetime]] = {}
    for start, end in [(e.start, e.end) for e in events] + [
        (b.start, b.end) for b in travel if b.end > b.start
    ]:
        day = start.astimezone(tz).date()
        low, high = spans.get(day, (start, end))
        spans[day] = (min(low, start, key=utc), max(high, end, key=utc))
    return list(spans.values())


def _at_base(
    request: TrainingRequest, away: list[tuple[datetime, datetime]]
) -> tuple[Interval, ...] | None:
    """Where a session without a place may go: at the residence in force, i.e. outside the
    outings to fixed commitments (and inside its category windows when it has some)."""
    if request.place is not None:
        return request.allowed
    windows = request.allowed or (Interval(request.earliest_start, request.latest_end),)
    return tuple(part for window in windows for part in subtract(window, away))


def _presence(commitment: Commitment, tolerance: timedelta) -> LocatedEvent:
    """Where and when the user must be: a class allows arriving ``tolerance`` late and leaving
    ``tolerance`` early (never down to nothing); other commitments are exact."""
    assert commitment.place is not None  # noqa: S101 - callers keep located commitments only
    start, end = commitment.start, commitment.end
    if (
        commitment.kind is CommitmentKind.CLASS
        and not commitment.exact_times
        and end - start > 2 * tolerance
    ):
        start, end = start + tolerance, end - tolerance
    return LocatedEvent(start, end, commitment.place)


def _worth_going_back(
    previous: LocatedEvent,
    following: LocatedEvent,
    base: Place | None,
    routing: RoutingProvider,
    buffer: timedelta,
) -> bool:
    """Go back to the base between two commitments only when, after both real journeys, at
    least ``MIN_TIME_AT_BASE`` remains there; otherwise travel directly between them."""
    if base is None or base in (previous.place, following.place):
        return False
    deadline = following.start - buffer
    if utc(deadline) - utc(previous.end) < MIN_TIME_AT_BASE:
        return False
    try:
        back = find_trips(
            routing,
            TripQuery(
                previous.place.coordinates,
                base.coordinates,
                depart_after=previous.end,
                arrive_before=deadline,
                objective=Objective.EARLIEST_ARRIVAL,
            ),
        ).best
        there = find_trips(
            routing,
            TripQuery(
                base.coordinates,
                following.place.coordinates,
                depart_after=previous.end,
                arrive_before=deadline,
                objective=Objective.LATEST_DEPARTURE,
            ),
        ).best
    except RoutingUnavailableError:
        return False  # direct trip: the outage is reported when it is routed
    return (
        back is not None
        and there is not None
        and utc(there.departure) - utc(back.arrival) >= MIN_TIME_AT_BASE
    )


def _route_days(
    located: list[LocatedEvent],
    data: PlanningInput,
    routing: RoutingProvider,
    issues: list[str],
    warnings: list[str],
) -> list[TravelBlock]:
    """Mandatory journeys of every local day: outings from and back to the base in force."""
    tz, now = data.timezone, utc(data.now)

    def base(at: datetime) -> Place | None:
        return base_at(at, data.stays, data.home, data.alternation)

    travel: list[TravelBlock] = []
    days: dict[date, list[LocatedEvent]] = {}
    for event in located:
        days.setdefault(event.start.astimezone(tz).date(), []).append(event)
    for events in (days[d] for d in sorted(days)):
        ordered = sorted(events, key=lambda e: utc(e.start))
        outings: list[list[LocatedEvent]] = [[ordered[0]]]
        for event in ordered[1:]:
            previous = outings[-1][-1]
            if _worth_going_back(previous, event, base(previous.end), routing, data.buffer):
                outings.append([event])
            else:
                outings[-1].append(event)
        for index, outing in enumerate(outings):
            first, last = outing[0], max(outing, key=lambda e: utc(e.end))
            start_base, end_base = base(first.start), base(last.end)
            for when, where in ((first.start, start_base), (last.end, end_base)):
                if where is None:
                    issues.append(
                        f"planned locations overlap at {when.isoformat(timespec='minutes')}: "
                        "travel origin unknown (say where you will be)"
                    )
            # Already under way: the departure check (worker) handles the current trip.
            start_place = None if utc(first.start) <= now else start_base
            following = outings[index + 1] if index + 1 < len(outings) else None
            plan = plan_day_travel(
                outing,
                routing,
                buffer=data.buffer,
                start_place=start_place,
                end_place=end_base,
                return_by=None if following is None else min(e.start for e in following),
            )
            travel += plan.blocks
            issues += [i for i in plan.issues if not i.startswith("origin unknown")]
            warnings += [w for w in plan.warnings if w not in warnings]
    return travel


def _busy(blocks: list[TravelBlock]) -> list[Interval]:
    return [Interval(b.start, b.end) for b in blocks if utc(b.end) > utc(b.start)]


def run_planning(data: PlanningInput, routing: RoutingProvider) -> PlanningOutcome:
    routing = CachedRouting(routing)  # one planning run: identical requests reach MOTIS once
    tz, now = data.timezone, utc(data.now)
    horizon = Interval(now, utc(data.horizon_end))
    issues: list[str] = []
    warnings: list[str] = []

    fixed = [c for c in data.commitments if utc(c.end) > now and utc(c.start) < horizon.end]
    questions: list[str] = []
    for c in fixed:
        if c.place is None and c.location_text:
            issues.append(
                f"unknown location '{c.location_text}' for '{c.title}': add a place alias "
                "(time blocked, travel not planned)"
            )
            questions.append(
                f"Où a lieu « {c.title} » (lieu indiqué : « {c.location_text} ») ? Donnez le "
                "lieu ou son adresse pour que je planifie le trajet."
            )
    fixed_busy = [Interval(c.start, c.end) for c in fixed]
    conflicts = find_conflicts(fixed, data.locked_blocks, data.now)
    fixed_located = [_presence(c, data.class_tolerance) for c in fixed if c.place is not None]
    residences = {data.home.id} | (
        set() if data.alternation is None else {p.id for p in data.alternation.residences}
    )
    elsewhere = [s for s in data.stays if s.place.id not in residences]
    # Journeys between fixed commitments first: flexible activities are placed around them.
    fixed_travel = _route_days(fixed_located, data, routing, [], [])
    # Sessions without a place happen at the residence: never during an outing (B08).
    away = _away_spans(fixed_located, fixed_travel, data.timezone)
    # A declared stay away from the residences (e.g. a night at a friend's) is away too.
    away += [(s.start, s.end) for s in elsewhere]

    # Flexible activities (trainings, personal outings, located task sessions)
    flexible: list[FlexibleActivity] = []
    by_id = {t.activity_id: t for t in data.trainings}
    places_ok = {t.activity_id: _at_base(t, away) for t in data.trainings}
    for request in data.trainings:
        if utc(request.latest_end) <= now:
            continue
        anchor = request.preferred_start or max(request.earliest_start, data.now, key=utc)
        before, after = _activity_travel(
            request, base_at(anchor, data.stays, data.home, data.alternation), routing, issues
        )
        if before:  # the real journey arrives ``buffer`` early: reserve that margin too
            before += data.buffer
        earliest = max(request.earliest_start, data.now, key=utc) + before
        if utc(earliest) >= utc(request.latest_end):
            earliest = max(request.earliest_start, data.now, key=utc)
        flexible.append(
            FlexibleActivity(
                activity_id=request.activity_id,
                title=request.title,
                duration=request.duration,
                earliest_start=earliest,
                latest_end=request.latest_end,
                preferred_start=request.preferred_start,
                previous_start=request.previous_start,
                travel_before=before,
                travel_after=after,
                preferred_day=request.preferred_day,
                sequence_group=request.sequence_group,
                sequence_index=request.sequence_index,
                min_days_after_previous=request.min_days_after_previous,
                allowed=places_ok[request.activity_id],
                soft_target=None if request.strict else request.target,
                training=request.training,
            )
        )
    day_start, day_end = data.activity_hours
    activity_plan = plan_activities(
        flexible,
        fixed_busy + _busy(fixed_travel),
        tz,
        day_start=day_start,
        day_end=day_end,
        training=data.training_preferences,
        class_days=frozenset(
            c.start.astimezone(tz).date() for c in fixed if c.kind is CommitmentKind.CLASS
        ),
        sun=_daylight(data.home),
    )
    if activity_plan.inconclusive:
        issues.append(
            f"flexible activities: no answer from the solver ({activity_plan.solver_status}): "
            "inconclusive, not a proof of impossibility; the previous plan is kept"
        )
        return PlanningOutcome(
            OutcomeStatus.NO_SOLUTION, None, issues=issues, conflicts=conflicts, questions=questions
        )
    for missing in activity_plan.unplaced:
        request = by_id[missing]
        where = " à la résidence" if request.place is None else ""
        rule = " et ses plages horaires" if request.allowed is not None else ""
        issues.append(
            f"« {request.title} » : aucun créneau libre{where} dans sa période{rule} sans "
            "enfreindre les contraintes (trajets compris) ; proposition : un autre jour, une "
            "période plus large ou une séance plus courte (à dire à l'assistant)"
        )
    for outside in activity_plan.outside_hours:
        warnings.append(f"activity '{by_id[outside].title}' placed outside the activity hours")

    # Day-by-day mandatory travel (fixed commitments + placed activities)
    located = fixed_located + [
        LocatedEvent(p.start, p.end, place)
        for p in activity_plan.placed
        if (place := by_id[p.activity_id].place) is not None
    ]
    travel = _route_days(located, data, routing, issues, warnings)

    # Study windows reduced by travel
    windows = travel_aware_study_windows(data.availability, located, routing, buffer=data.buffer)
    issues += windows.issues
    availability = [
        Interval(max(w.start, data.now, key=utc), w.end)
        for w in windows.windows
        if utc(w.end) > now and utc(max(w.start, data.now, key=utc)) < utc(w.end)
    ]
    busy = fixed_busy + [b.reserved for b in activity_plan.placed] + _busy(travel)
    problem = PlanningProblem(
        horizon=horizon,
        timezone=tz,
        availability=availability,
        busy=busy,
        demands=data.demands,
        locked_blocks=[b for b in data.locked_blocks if utc(b.end) > now],
        preferences=data.preferences,
        previous_blocks=[b for b in data.previous_blocks if utc(b.start) >= now],
    )
    result = solve(problem, time_limit_seconds=data.solver_seconds)
    outcome = PlanningOutcome(
        status=OutcomeStatus(result.status.value),
        result=result,
        trainings=activity_plan.placed,
        unplaced_trainings=activity_plan.unplaced,
        travel=travel,
        issues=issues,
        warnings=warnings,
        unplanned_minutes=dict(result.unplanned_minutes),
        late_minutes=dict(result.late_minutes),
        hard_deadline_misses=list(result.hard_deadline_misses),
        outside_hours=activity_plan.outside_hours,
        conflicts=conflicts,
        questions=questions,
    )
    if result.status is PlanStatus.NO_SOLUTION:
        issues.append(
            f"study: no answer within the solver time limit ({result.solver_status}): "
            "inconclusive, the previous plan is kept"
        )
        return outcome
    _task_sessions(data, activity_plan, outcome)
    _deadline_capacity(data, problem, outcome)

    placed = {p.activity_id: p for p in activity_plan.placed}
    sessions = [
        PlacedSession(
            ref=f"activity:{p.activity_id}",
            title=by_id[p.activity_id].title,
            start=p.start,
            end=p.end,
            earliest_start=by_id[p.activity_id].earliest_start,
            latest_end=by_id[p.activity_id].latest_end,
            allowed=places_ok[p.activity_id],  # the same rule, checked independently
            strict_target=by_id[p.activity_id].target if by_id[p.activity_id].strict else None,
            sequence_group=by_id[p.activity_id].sequence_group,
            sequence_index=by_id[p.activity_id].sequence_index,
            min_days_after_previous=by_id[p.activity_id].min_days_after_previous,
        )
        for p in placed.values()
    ]
    occupied = [Occupied(c.title, Interval(c.start, c.end)) for c in fixed]
    routed = [
        Occupied(f"{b.origin.name} -> {b.destination.name}", Interval(b.start, b.end))
        for b in travel
        if b.status is TravelStatus.ROUTED and utc(b.end) > utc(b.start)
    ]
    edges = {utc(p.start) for p in placed.values()} | {utc(p.end) for p in placed.values()}
    chosen = [
        trip
        for trip, block in zip(
            routed,
            (b for b in travel if b.status is TravelStatus.ROUTED and utc(b.end) > utc(b.start)),
            strict=True,
        )
        if utc(block.end) in edges or utc(block.start) in edges
    ]
    violations = validate_plan(problem, result) + validate_schedule(
        sessions, occupied, routed, tz, chosen
    )
    if violations:
        outcome.status, outcome.violations = OutcomeStatus.INVALID, violations
    elif (
        outcome.unplanned_minutes
        or outcome.hard_deadline_misses
        or activity_plan.unplaced
        or any(b.status is TravelStatus.IMPOSSIBLE for b in travel)
    ):
        outcome.status = OutcomeStatus.PARTIAL
    return outcome


def _task_sessions(data: PlanningInput, activity_plan: Any, outcome: PlanningOutcome) -> None:
    """Results of located / non-splittable task sessions, reported like study tasks."""
    placed = {p.activity_id: p for p in activity_plan.placed}
    for request in data.trainings:
        if request.task_id is None or utc(request.latest_end) <= utc(data.now):
            continue
        minutes = int(request.duration.total_seconds() // 60)
        spot = placed.get(request.activity_id)
        if spot is None:
            outcome.unplanned_minutes[request.task_id] = minutes
            if request.strict:
                outcome.hard_deadline_misses.append(request.task_id)
        elif request.target is not None and utc(spot.end) > utc(request.target):
            outcome.late_minutes[request.task_id] = minutes
    for task_id, minutes in data.expired_strict_tasks.items():
        outcome.unplanned_minutes[task_id] = minutes
        outcome.hard_deadline_misses.append(task_id)
        outcome.issues.append(f"task {task_id}: strict deadline already passed with work left")


def _deadline_capacity(
    data: PlanningInput, problem: PlanningProblem, outcome: PlanningOutcome
) -> None:
    by_task = {d.task_id: d for d in data.demands}
    for task_id in outcome.hard_deadline_misses:
        demand = by_task.get(task_id)
        if demand is None:
            continue
        outcome.deadline_capacity[task_id] = {
            "deadline": (demand.target - timedelta(minutes=1))
            .astimezone(data.timezone)
            .date()
            .isoformat(),
            "deadline_end": demand.target.isoformat(),
            "needed_minutes": demand.remaining_minutes,
            "free_minutes_before_deadline": free_minutes(
                problem.availability, problem.busy, demand.target
            ),
        }
