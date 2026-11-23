"""Deterministic planning pipeline: fixed events -> trainings -> travel -> study.

Pure function over plain inputs (no database, no clock), so it can be tested exhaustively and
re-run by the worker. Rules (docs/REQUIREMENTS.md, docs/SCHEDULER.md, docs/TRANSPORT.md):

1. University events, races and locked trainings are fixed. An event whose location text is
   not a known place/alias still blocks its time but gets no travel: reported, never guessed.
2. Flexible trainings move inside their window (``scheduler.activities``) around fixed events;
   travel home <-> training place is reserved around them. When no journey is known the
   reservation is conservative (``UNROUTED_TRAINING_TRAVEL``) and reported.
3. Each local day starts and ends at the day's base place: a declared stay covering local noon,
   otherwise home. There is no automatic switching between family homes.
4. Mandatory travel between located commitments is routed (``transport.travel``); impossible
   or unknown journeys block the gap and are reported. A day is split into outings: when two
   commitments are more than ``RETURN_HOME_GAP`` apart the user returns to the base between them.
5. Study availability windows (with their place) are reduced by travel to/from commitments.
6. CP-SAT places study; the independent validator must accept the plan, otherwise nothing is
   proposed (``status = invalid``).
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
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
from tempo.transport.location import DeclaredStay, LocatedEvent
from tempo.transport.places import Place
from tempo.transport.routing import RoutingProvider, RoutingUnavailableError
from tempo.transport.travel import (
    DEFAULT_BUFFER,
    StudyWindow,
    TravelBlock,
    plan_day_travel,
    travel_aware_study_windows,
)

UNROUTED_TRAINING_TRAVEL = timedelta(minutes=90)
RETURN_HOME_GAP = timedelta(hours=3)
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


def _base_place(day: date, data: PlanningInput) -> Place:
    noon = datetime.combine(day, time(12), tzinfo=data.timezone).astimezone(UTC)
    covering = [s for s in data.stays if utc(s.start) <= noon < utc(s.end)]
    return covering[0].place if len(covering) == 1 else data.home


def _training_travel(
    request: TrainingRequest, base: Place, routing: RoutingProvider, issues: list[str]
) -> tuple[timedelta, timedelta]:
    if request.place is None or request.place == base:
        return timedelta(0), timedelta(0)
    anchor = request.preferred_start or request.earliest_start
    try:
        there = routing.plan(base.coordinates, request.place.coordinates, anchor, arrive_by=True)
        back = routing.plan(
            request.place.coordinates, base.coordinates, anchor + request.duration, arrive_by=False
        )
    except RoutingUnavailableError as exc:
        issues.append(f"training '{request.title}': routing unavailable ({exc}); travel reserved")
        return UNROUTED_TRAINING_TRAVEL, UNROUTED_TRAINING_TRAVEL
    before = min((j.duration for j in there), default=None)
    after = min((j.duration for j in back), default=None)
    if before is None or after is None:
        issues.append(
            f"training '{request.title}': no journey to/from {request.place.name}; travel reserved"
        )
    return (
        UNROUTED_TRAINING_TRAVEL if before is None else before,
        UNROUTED_TRAINING_TRAVEL if after is None else after,
    )


def _outings(events: list[LocatedEvent]) -> list[list[LocatedEvent]]:
    ordered = sorted(events, key=lambda e: utc(e.start))
    outings: list[list[LocatedEvent]] = [[ordered[0]]]
    for event in ordered[1:]:
        if utc(event.start) - utc(outings[-1][-1].end) > RETURN_HOME_GAP:
            outings.append([event])
        else:
            outings[-1].append(event)
    return outings


def run_planning(data: PlanningInput, routing: RoutingProvider) -> PlanningOutcome:
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

    # 2. trainings
    flexible: list[FlexibleActivity] = []
    by_id = {t.activity_id: t for t in data.trainings}
    for request in data.trainings:
        if utc(request.latest_end) <= now:
            continue
        base = _base_place(request.earliest_start.astimezone(tz).date(), data)
        before, after = _training_travel(request, base, routing, issues)
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
            )
        )
    activity_plan = plan_activities(flexible, fixed_busy, tz)
    for missing in activity_plan.unplaced:
        issues.append(f"training '{by_id[missing].title}': no free slot in its window")

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
        base = _base_place(day, data)
        for outing in _outings(events):
            first_start = min(utc(e.start) for e in outing)
            # Already under way: the departure check (worker) handles the current trip.
            start_place: Place | None = None if first_start <= now else base
            plan = plan_day_travel(
                outing, routing, buffer=data.buffer, start_place=start_place, end_place=base
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
    if result.status is not PlanStatus.NO_SOLUTION:
        violations = validate_plan(problem, result)
        if violations:
            outcome.status, outcome.violations = OutcomeStatus.INVALID, violations
    return outcome
