"""Independent deterministic validation of a whole plan against its HARD rules.

Kept separate from the solvers on purpose: production code runs it on every planning output,
and a plan with violations is never published (CLAUDE.md: deterministic code verifies rules).
Soft preferences (daily study cap, blocks per subject, activity hours, suggested days) are
never violations: they are penalised by the solvers and reported.

Checked: study blocks (``validate_plan``) and every other planned item (``validate_schedule``):
flexible activities inside their window, their category windows and before a strict target;
the order and recovery days of a coach week; no overlap between flexible activities, fixed
commitments and routed journeys.
"""

import itertools
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from tempo.domain.models import TargetKind
from tempo.scheduler.model import (
    Interval,
    PlannedBlock,
    PlanningProblem,
    PlanResult,
    PlanStatus,
    utc,
)


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


def _check_block(problem: PlanningProblem, block: PlannedBlock) -> list[str]:
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
    if any(busy.overlaps(block.interval) for busy in problem.busy):
        found.append(f"block overlaps a busy interval: {label}")
    if demand.target_kind is TargetKind.HARD and end > utc(demand.target):
        found.append(f"block after hard deadline: {label}")
    return found


def validate_plan(problem: PlanningProblem, result: PlanResult) -> list[str]:
    prefs = problem.preferences
    demands = {d.task_id: d for d in problem.demands}
    violations: list[str] = []

    for locked in problem.locked_blocks:
        if locked not in result.blocks:
            violations.append(f"locked block dropped or moved: {_label(locked)}")
    for block in result.blocks:
        if not block.locked:
            violations.extend(_check_block(problem, block))

    ordered = sorted(result.blocks, key=lambda b: utc(b.start))
    for i, first in enumerate(ordered):
        for second in ordered[i + 1 :]:
            if utc(second.start) >= utc(first.end):
                continue
            if not (first.locked and second.locked):
                violations.append(f"blocks overlap: {_label(first)} / {_label(second)}")

    planned: defaultdict[str, int] = defaultdict(int)
    for block in result.blocks:
        planned[block.task_id] += _minutes(block)

    for task_id, total in planned.items():
        # The solver may round the last block up to the grid (overshoot <= one minimum block).
        demand = demands.get(task_id)
        if demand is not None and total > demand.remaining_minutes + prefs.min_block_minutes:
            violations.append(f"{task_id}: {total} min planned, more than requested")
    if result.status is PlanStatus.COMPLETE:
        for demand in problem.demands:
            if planned[demand.task_id] < demand.remaining_minutes:
                violations.append(f"{demand.task_id}: status complete but effort is missing")
    return violations


def busy_conflicts(problem: PlanningProblem) -> list[str]:
    """Locked blocks that collide with fixed commitments: protected changes needing approval."""
    return [
        f"locked block conflicts with a fixed commitment: {_label(locked)}"
        for locked in problem.locked_blocks
        if any(b.overlaps(locked.interval) for b in problem.busy)
    ]


@dataclass(frozen=True, slots=True)
class PlacedSession:
    """A flexible activity as placed by the activity planner, with the rules it must obey."""

    ref: str
    title: str
    start: datetime
    end: datetime
    earliest_start: datetime
    latest_end: datetime
    allowed: tuple[Interval, ...] | None = None
    strict_target: datetime | None = None
    sequence_group: str | None = None
    sequence_index: int = 0
    min_days_after_previous: int = 0


@dataclass(frozen=True, slots=True)
class Occupied:
    """Time a planned item takes (fixed commitment or routed journey)."""

    label: str
    interval: Interval


def _overlap(a: Interval, b: Interval) -> bool:
    return utc(a.start) < utc(b.end) and utc(b.start) < utc(a.end)


def validate_schedule(
    sessions: Sequence[PlacedSession],
    fixed: Sequence[Occupied],
    journeys: Sequence[Occupied],
    timezone: ZoneInfo,
    chosen_journeys: Sequence[Occupied] = (),
) -> list[str]:
    """Hard rules of everything that is not a study block (see the module docstring).

    Overlaps between FIXED commitments (or the journeys linking them) are source conflicts,
    reported for arbitration, not plan violations: the planner did not choose them. Journeys
    to or from a flexible activity (``chosen_journeys``) follow from the planner's choice, so
    they must not overlap any fixed commitment either."""
    found: list[str] = []
    for s in sessions:
        span = Interval(s.start, s.end)
        label = f"{s.title} {s.start.isoformat()}"
        if utc(s.start) < utc(s.earliest_start) or utc(s.end) > utc(s.latest_end):
            found.append(f"activity outside its window: {label}")
        if s.allowed is not None and not any(w.contains(span) for w in s.allowed):
            found.append(f"activity outside its category windows: {label}")
        if s.strict_target is not None and utc(s.end) > utc(s.strict_target):
            found.append(f"activity after its strict deadline: {label}")
        for item in fixed:
            if _overlap(span, item.interval):
                found.append(f"activity overlaps {item.label}: {label}")
        for trip in journeys:
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
                found.append(f"coach order broken: {after.title} before {before.title}")
            gap = (
                after.start.astimezone(timezone).date() - before.start.astimezone(timezone).date()
            ).days
            if gap < after.min_days_after_previous:
                found.append(f"recovery too short before {after.title} ({gap} day(s))")
    for trip in chosen_journeys:
        for item in fixed:
            if _overlap(trip.interval, item.interval):
                found.append(f"journey {trip.label} overlaps {item.label}")
    return found
