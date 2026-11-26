"""Independent deterministic validation of a whole plan against its HARD rules.

Kept separate from the solver on purpose: it runs on every planning output, on the FINAL plan
(the journeys actually routed by MOTIS after placement, not the estimates the solver used), and
a plan with violations is never published. Soft preferences (activity hours, darkness, days,
breaks, alternation, distribution, stability) are never violations: they are penalised by the
solver and reported.

Checked: study blocks (``validate_plan``): horizon, grid, length bounds, study availability,
no overlap with busy time or activities, deadlines (soft or strict: never exceeded without the
user's approval), kept fixed blocks, volume. Everything else (``validate_schedule``): flexible
activities inside their window and allowed intervals, before their deadline, not where the
user cannot be (stay elsewhere, unknown location), coach order and explicit recovery, no
overlap with commitments, journeys, other activities or study; every journey to or from a
flexible activity is feasible (routed, or a previous itinerary kept during an outage) and
does not overlap a commitment.
"""

import itertools
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tempo.scheduler.model import PlannedBlock, PlanningProblem, PlanResult, PlanStatus
from tempo.timeutil import Interval, utc


def _minutes(block: PlannedBlock) -> int:
    return int(block.duration.total_seconds() // 60)


def _label(block: PlannedBlock) -> str:
    return f"{block.task_id} {block.start.isoformat()}-{block.end.isoformat()}"


def _merged(windows: list[Interval]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted((utc(w.start), utc(w.end)) for w in windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlap(a: Interval, b: Interval) -> bool:
    return utc(a.start) < utc(b.end) and utc(b.start) < utc(a.end)


def _check_block(problem: PlanningProblem, block: PlannedBlock, taken: list[Interval]) -> list[str]:
    prefs = problem.preferences
    label = _label(block)
    demand = next((d for d in problem.demands if d.task_id == block.task_id), None)
    if demand is None:
        return [f"block for unknown task: {label}"]
    found: list[str] = []
    if not problem.horizon.contains(block.interval):
        found.append(f"block outside horizon: {label}")
    length = _minutes(block)
    if not prefs.min_block_minutes <= length <= prefs.max_block_minutes:
        found.append(f"invalid block duration {length} min: {label}")
    local = block.start.astimezone(problem.timezone)
    g = prefs.granularity_minutes
    if local.minute % g or local.second or local.microsecond or length % g:
        found.append(f"block off the {g}-minute grid: {label}")
    start, end = utc(block.start), utc(block.end)
    if not any(s <= start and end <= e for s, e in _merged(problem.availability)):
        found.append(f"block outside availability: {label}")
    if any(_overlap(busy, block.interval) for busy in [*problem.busy, *taken]):
        found.append(f"block overlaps a busy interval: {label}")
    if demand.target is not None and end > utc(demand.target):
        found.append(f"block after the deadline: {label}")
    if demand.not_before is not None and start < utc(demand.not_before):
        found.append(f"block before its allowed start: {label}")
    return found


def validate_plan(problem: PlanningProblem, result: PlanResult) -> list[str]:
    """Study blocks of the final plan (``taken``: placed activities with their travel)."""
    prefs = problem.preferences
    demands = {d.task_id: d for d in problem.demands}
    violations: list[str] = []
    taken = [a.reserved for a in result.activities]
    for kept in problem.fixed_blocks:
        if kept not in result.blocks:
            violations.append(f"fixed block dropped or moved: {_label(kept)}")
    for block in result.blocks:
        if block not in problem.fixed_blocks:
            violations.extend(_check_block(problem, block, taken))
    ordered = sorted(result.blocks, key=lambda b: utc(b.start))
    for i, first in enumerate(ordered):
        for second in ordered[i + 1 :]:
            if utc(second.start) >= utc(first.end):
                break
            if not (first in problem.fixed_blocks and second in problem.fixed_blocks):
                violations.append(f"blocks overlap: {_label(first)} / {_label(second)}")
    planned: defaultdict[str, int] = defaultdict(int)
    for block in result.blocks:
        planned[block.task_id] += _minutes(block)
    for task_id, total in planned.items():
        demand = demands.get(task_id)
        # The last block may be rounded up to the grid (overshoot <= one minimum block).
        if demand is not None and total > demand.remaining_minutes + prefs.min_block_minutes:
            violations.append(f"{task_id}: {total} min planned, more than requested")
    if result.status is PlanStatus.COMPLETE:
        for demand in problem.demands:
            if planned[demand.task_id] < demand.remaining_minutes:
                violations.append(f"{demand.task_id}: status complete but effort is missing")
    return violations


@dataclass(frozen=True, slots=True)
class PlacedSession:
    """A flexible activity as placed, with the hard rules it must obey."""

    ref: str
    title: str
    start: datetime
    end: datetime
    earliest_start: datetime
    latest_end: datetime
    allowed: tuple[Interval, ...] | None = None
    deadline: datetime | None = None
    sequence_group: str | None = None
    sequence_index: int = 0
    training: bool = False
    rest_days: int = 0
    """Explicit recovery (days) required after the previous training."""


@dataclass(frozen=True, slots=True)
class Occupied:
    """Time a planned item takes (commitment, journey, study block)."""

    label: str
    interval: Interval


@dataclass(frozen=True, slots=True)
class Trip:
    """A journey of the final plan, with the flexible session it serves (if any)."""

    label: str
    interval: Interval
    feasible: bool
    """Routed, or a previous itinerary kept during an outage."""
    serves: str | None = None
    """``activity:<id>`` reached or left by this journey (None: fixed commitments only)."""


def validate_schedule(
    sessions: Sequence[PlacedSession],
    fixed: Sequence[Occupied],
    trips: Sequence[Trip],
    timezone: ZoneInfo,
    *,
    study: Sequence[Occupied] = (),
    away: Sequence[Interval] = (),
    at_home: frozenset[str] = frozenset(),
    sport_days: Sequence[datetime] = (),
) -> list[str]:
    """Hard rules of everything that is not a study block (module docstring).

    Overlaps between FIXED commitments (or the journeys linking them) are source conflicts,
    reported for arbitration, not plan violations: the planner did not choose them."""
    found: list[str] = []
    for s in sessions:
        span = Interval(s.start, s.end)
        label = f"{s.title} {s.start.isoformat()}"
        if utc(s.start) < utc(s.earliest_start) or utc(s.end) > utc(s.latest_end):
            found.append(f"activity outside its window: {label}")
        if s.allowed is not None and not any(w.contains(span) for w in s.allowed):
            found.append(f"activity outside its allowed periods: {label}")
        if s.deadline is not None and utc(s.end) > utc(s.deadline):
            found.append(f"activity after its deadline: {label}")
        if s.ref in at_home and any(_overlap(span, a) for a in away):
            found.append(f"activity at the residence while the user is away: {label}")
        for item in [*fixed, *study]:
            if _overlap(span, item.interval):
                found.append(f"activity overlaps {item.label}: {label}")
        for trip in trips:
            if _overlap(span, trip.interval):
                found.append(f"activity overlaps journey {trip.label}: {label}")
    ordered = sorted(sessions, key=lambda x: utc(x.start))
    for i, first in enumerate(ordered):
        for second in ordered[i + 1 :]:
            if utc(second.start) >= utc(first.end):
                break
            found.append(f"activities overlap: {first.title} / {second.title}")
    groups: defaultdict[str, list[PlacedSession]] = defaultdict(list)
    for s in sessions:
        if s.sequence_group is not None:
            groups[s.sequence_group].append(s)
    for members in groups.values():
        members.sort(key=lambda x: x.sequence_index)
        for before, after in itertools.pairwise(members):
            if utc(after.start) < utc(before.end):
                found.append(f"order broken: {after.title} before {before.title}")
    sport: list[tuple[datetime, PlacedSession | None]] = [
        (s.start, s) for s in sessions if s.training
    ] + [(d, None) for d in sport_days]
    sport.sort(key=lambda x: utc(x[0]))
    for (earlier, _), (later, session) in itertools.pairwise(sport):
        if session is None or not session.rest_days:
            continue
        gap = (later.astimezone(timezone).date() - earlier.astimezone(timezone).date()).days
        if gap < session.rest_days:
            found.append(f"recovery too short before {session.title} ({gap} day(s))")
    for trip in trips:
        for block in study:
            if _overlap(trip.interval, block.interval):
                found.append(f"journey {trip.label} overlaps {block.label}")
        if trip.serves is None:
            continue
        if not trip.feasible:
            found.append(f"journey {trip.label} for {trip.serves} is not feasible")
        for item in fixed:
            if _overlap(trip.interval, item.interval):
                found.append(f"journey {trip.label} overlaps {item.label}")
        for other in sessions:
            if other.ref != trip.serves and _overlap(
                trip.interval, Interval(other.start, other.end)
            ):
                found.append(f"journey {trip.label} overlaps {other.title}")
    return found


@dataclass(frozen=True, slots=True)
class Stop:
    """Where the user must be: a located commitment or activity of the final plan."""

    label: str
    place: str
    interval: Interval
    tolerance: timedelta = timedelta(0)
    """Classes only: how much a journey may overlap its start or end (never an exam)."""


@dataclass(frozen=True, slots=True)
class Leg:
    """A journey of the final plan, with its places."""

    label: str
    origin: str
    destination: str
    interval: Interval
    feasible: bool = True
    """False: impossible or unknown (a reserved gap, reported elsewhere): it still moves the
    user from origin to destination, but is not a journey to check."""


def validate_journeys(legs: Sequence[Leg], stops: Sequence[Stop], timezone: ZoneInfo) -> list[str]:
    """The journeys of the final plan are physically possible: no two at once, each leaves
    from where the user is (previous stop or arrival) and leads to the next stop, and a
    journey overlaps a class only within its attendance tolerance."""
    found: list[str] = []
    ordered = sorted((g for g in legs if g.feasible), key=lambda leg: utc(leg.interval.start))
    for first, second in itertools.pairwise(ordered):
        if utc(second.interval.start) < utc(first.interval.end):
            found.append(f"journeys overlap: {first.label} / {second.label}")
    for leg in ordered:
        for stop in stops:
            span, trip = stop.interval, leg.interval
            if not _overlap(span, trip):
                continue
            late = utc(trip.end) - utc(span.start) if utc(trip.start) <= utc(span.start) else None
            early = utc(span.end) - utc(trip.start) if utc(trip.end) >= utc(span.end) else None
            within = (late is not None and late <= stop.tolerance) or (
                early is not None and early <= stop.tolerance
            )
            if not within:
                found.append(f"journey {leg.label} overlaps {stop.label}")
    # Two commitments at once (different places) are a source conflict, reported for
    # arbitration: where the user is then is unknown, never a journey error.
    conflicted = {
        id(a)
        for a in stops
        for b in stops
        if a is not b and a.place != b.place and _overlap(a.interval, b.interval)
    }
    items: list[tuple[datetime, datetime, str, str | None, str, bool]] = [
        (s.interval.start, s.interval.end, s.place, None, s.label, id(s) in conflicted)
        for s in stops
    ] + [(g.interval.start, g.interval.end, g.origin, g.destination, g.label, False) for g in legs]
    items.sort(key=lambda item: (utc(item[0]), item[3] is None))
    where: str | None = None
    day = None
    for start, _end, place, destination, label, conflict in items:
        local_day = start.astimezone(timezone).date()
        if local_day != day:  # a new day starts from its base (checked by the routing)
            day, where = local_day, None
        if destination is None:  # a stop
            if conflict:
                where = None
                continue
            if where is not None and where != place:
                found.append(f"{label} at {place} but the user is at {where}")
            where = place
        else:  # a journey
            if where is not None and where != place:
                found.append(f"journey {label} leaves {place} but the user is at {where}")
            where = destination
    return found
