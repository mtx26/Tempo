"""Deterministic planning pipeline: fixed events -> trainings -> travel -> study.

Pure function over plain inputs (no database, no clock), so it can be tested exhaustively and
re-run by the worker. Rules (ARCHITECTURE.md §7-8):

1. University events, races and locked trainings are fixed. An event whose location text is
   not a known place/alias still blocks its time but gets no travel: reported, never guessed.
2. Flexible trainings move inside their window (``scheduler.activities``) around fixed events;
   travel home <-> training place is reserved around them. When no journey is known the
   reservation is conservative (``UNROUTED_TRAINING_TRAVEL``) and reported.
3. Each local day starts and ends at the day's base place: a declared stay covering local noon,
   otherwise home. There is no automatic switching between family homes.
4. Mandatory travel between located commitments is routed (``transport.travel``); impossible
   or unknown journeys block the gap and are reported. A day is split into outings: the user
   goes back to the base between two commitments only when at least ``MIN_TIME_AT_BASE``
   remains there after both real journeys; otherwise the journey goes directly.
5. Study availability windows (with their place) are reduced by travel to/from commitments.
6. CP-SAT places study; the independent validator must accept the plan, otherwise nothing is
   proposed (``status = invalid``).
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from tempo.scheduler.activities import FlexibleActivity, PlacedActivity, plan_activities
from tempo.scheduler.model import (
    Interval,
    PlannedBlock,
    PlanningProblem,
    PlanResult,
    PlanStatus,
    Preferences,
    StudyDemand,
    utc,
)
from tempo.scheduler.solver import solve
from tempo.scheduler.validate import validate_plan
from tempo.transport.itinerary import CachedRouting, Objective, TripQuery, find_trips
from tempo.transport.location import DeclaredStay, LocatedEvent, day_base
from tempo.transport.places import Place
from tempo.transport.routing import RoutingProvider, RoutingUnavailableError
from tempo.transport.travel import (
    DEFAULT_BUFFER,
    MAX_TRAVEL,
    StudyWindow,
    TravelBlock,
    plan_day_travel,
    travel_aware_study_windows,
)

UNROUTED_TRAINING_TRAVEL = timedelta(minutes=90)
MIN_TIME_AT_BASE = timedelta(hours=1)
"""Between two commitments further apart than this, the user goes back to the day's base."""


class CommitmentKind(StrEnum):
    CLASS = "class"
    RACE = "race"
    TRAINING = "training"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Commitment:
    """A fixed commitment (iCal class, race, locked training...)."""

    ref: str
    title: str
    start: datetime
    end: datetime
    kind: CommitmentKind
    place: Place | None = None
    location_text: str | None = None


@dataclass(frozen=True, slots=True)
class TrainingRequest:
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
    preferences: Preferences = field(default_factory=Preferences)
    buffer: timedelta = DEFAULT_BUFFER
    solver_seconds: float = 10.0


class OutcomeStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NO_SOLUTION = "no_solution"
    INVALID = "invalid"
    """The validator rejected the solver's plan: nothing may be proposed."""


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
    """Travel time to reserve around a flexible activity before it is placed (estimate around
    its preferred time; the actual journeys are chosen once it is placed)."""
    if request.place is None or request.place == base:
        return timedelta(0), timedelta(0)
    if base is None:
        issues.append(f"activity '{request.title}': travel origin unknown; travel reserved")
        return UNROUTED_TRAINING_TRAVEL, UNROUTED_TRAINING_TRAVEL
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
        issues.append(f"activity '{request.title}': routing unavailable ({exc}); travel reserved")
        return UNROUTED_TRAINING_TRAVEL, UNROUTED_TRAINING_TRAVEL
    if there is None or back is None:
        issues.append(
            f"activity '{request.title}': no journey to/from {request.place.name}; travel reserved"
        )
    return (
        UNROUTED_TRAINING_TRAVEL if there is None else there.duration,
        UNROUTED_TRAINING_TRAVEL if back is None else back.duration,
    )


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


def _outings(
    events: list[LocatedEvent], base: Place | None, routing: RoutingProvider, buffer: timedelta
) -> list[list[LocatedEvent]]:
    ordered = sorted(events, key=lambda e: utc(e.start))
    outings: list[list[LocatedEvent]] = [[ordered[0]]]
    for event in ordered[1:]:
        if _worth_going_back(outings[-1][-1], event, base, routing, buffer):
            outings.append([event])
        else:
            outings[-1].append(event)
    return outings


def run_planning(data: PlanningInput, routing: RoutingProvider) -> PlanningOutcome:
    routing = CachedRouting(routing)  # one planning run: identical requests reach MOTIS once
    tz, now = data.timezone, utc(data.now)
    horizon = Interval(now, utc(data.horizon_end))
    issues: list[str] = []
    warnings: list[str] = []

    fixed = [c for c in data.commitments if utc(c.end) > now and utc(c.start) < horizon.end]
    for c in fixed:
        if c.place is None and c.location_text:
            issues.append(
                f"unknown location '{c.location_text}' for '{c.title}': add a place alias "
                "(time blocked, travel not planned)"
            )
    fixed_busy = [Interval(c.start, c.end) for c in fixed]

    def base_of(day: date) -> Place | None:
        return day_base(day, data.stays, data.home, tz)

    # 2. flexible activities (trainings, personal, located task sessions)
    flexible: list[FlexibleActivity] = []
    by_id = {t.activity_id: t for t in data.trainings}
    for request in data.trainings:
        if utc(request.latest_end) <= now:
            continue
        base = base_of(request.earliest_start.astimezone(tz).date())
        before, after = _activity_travel(request, base, routing, issues)
        flexible.append(
            FlexibleActivity(
                activity_id=request.activity_id,
                title=request.title,
                duration=request.duration,
                earliest_start=max(request.earliest_start, data.now, key=utc) + before,
                latest_end=request.latest_end,
                preferred_start=request.preferred_start,
                previous_start=request.previous_start,
                travel_before=before,
                travel_after=after,
                preferred_day=request.preferred_day,
                sequence_group=request.sequence_group,
                sequence_index=request.sequence_index,
                min_days_after_previous=request.min_days_after_previous,
                allowed=request.allowed,
            )
        )
    activity_plan = plan_activities(flexible, fixed_busy, tz)
    for missing in activity_plan.unplaced:
        rule = " and its category windows" if by_id[missing].allowed is not None else ""
        issues.append(f"activity '{by_id[missing].title}': no free slot in its window{rule}")
    # Independent check of the hard category windows (never trust the solver alone).
    category_violations = [
        f"activity '{by_id[p.activity_id].title}' placed outside its category windows"
        for p in activity_plan.placed
        if (allowed := by_id[p.activity_id].allowed) is not None
        and not any(utc(w.start) <= utc(p.start) and utc(p.end) <= utc(w.end) for w in allowed)
    ]

    # 3-4. day-by-day mandatory travel
    located: list[LocatedEvent] = [
        LocatedEvent(c.start, c.end, c.place) for c in fixed if c.place is not None
    ]
    located += [
        LocatedEvent(p.start, p.end, place)
        for p in activity_plan.placed
        if (place := by_id[p.activity_id].place) is not None
    ]
    travel: list[TravelBlock] = []
    days: dict[date, list[LocatedEvent]] = {}
    for event in located:
        days.setdefault(event.start.astimezone(tz).date(), []).append(event)
    for day, events in sorted(days.items()):
        base = base_of(day)
        if base is None:
            issues.append(
                f"planned locations overlap on {day.isoformat()}: travel origin unknown "
                "(say where you will be)"
            )
        outings = _outings(events, base, routing, data.buffer)
        for index, outing in enumerate(outings):
            first_start = min(utc(e.start) for e in outing)
            # Already under way: the departure check (worker) handles the current trip.
            start_place: Place | None = None if first_start <= now else base
            # The way back must be home before the next outing of the day starts.
            following = outings[index + 1] if index + 1 < len(outings) else None
            plan = plan_day_travel(
                outing,
                routing,
                buffer=data.buffer,
                start_place=start_place,
                end_place=base,
                return_by=None if following is None else min(e.start for e in following),
            )
            travel += plan.blocks
            issues += [i for i in plan.issues if not i.startswith("origin unknown")]
            warnings += plan.warnings

    # 5. study windows reduced by travel
    windows = travel_aware_study_windows(data.availability, located, routing, buffer=data.buffer)
    issues += windows.issues
    availability = [w for w in windows.windows if utc(w.end) > now]
    availability = [
        Interval(max(w.start, data.now, key=utc), w.end)
        for w in availability
        if utc(w.end) > now and utc(max(w.start, data.now, key=utc)) < utc(w.end)
    ]

    busy = (
        fixed_busy
        + [b.reserved for b in activity_plan.placed]
        + [Interval(b.start, b.end) for b in travel if utc(b.end) > utc(b.start)]
    )
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
    )
    if result.hard_deadline_misses:
        by_task = {d.task_id: d for d in data.demands}
        for task_id in result.hard_deadline_misses:
            demand = by_task.get(task_id)
            if demand is None:
                continue
            outcome.deadline_capacity[task_id] = {
                "deadline": (demand.target - timedelta(minutes=1))
                .astimezone(tz)
                .date()
                .isoformat(),
                "deadline_end": demand.target.isoformat(),
                "needed_minutes": demand.remaining_minutes,
                "free_minutes_before_deadline": free_minutes(
                    problem.availability, problem.busy, demand.target
                ),
            }
    if category_violations:
        outcome.status, outcome.violations = OutcomeStatus.INVALID, category_violations
    elif result.status is not PlanStatus.NO_SOLUTION:
        violations = validate_plan(problem, result)
        if violations:
            outcome.status, outcome.violations = OutcomeStatus.INVALID, violations
    return outcome
