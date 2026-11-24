"""Independent deterministic validation of a plan against hard rules.

Kept separate from the solver on purpose: production code runs it on every solver output, and
a plan with violations is never proposed (CLAUDE.md: deterministic code verifies rules).
"""

from collections import defaultdict
from datetime import date, datetime

from tempo.domain.models import TargetKind
from tempo.scheduler.model import (
    Interval,
    PlannedBlock,
    PlanningProblem,
    PlanResult,
    PlanStatus,
    utc,
)

EXTRA_BLOCKS_PER_SUBJECT = 2
"""Mirror of ``solver.EXTRA_BLOCKS_PER_SUBJECT`` (validator stays independent of the solver)."""


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
    daily: defaultdict[date, int] = defaultdict(int)
    daily_new: defaultdict[date, int] = defaultdict(int)
    per_subject_day: defaultdict[tuple[str, date], int] = defaultdict(int)
    for block in result.blocks:
        day = block.start.astimezone(problem.timezone).date()
        planned[block.task_id] += _minutes(block)
        daily[day] += _minutes(block)
        if not block.locked:
            daily_new[day] += _minutes(block)
            per_subject_day[(block.subject, day)] += 1

    for task_id, total in planned.items():
        demand = demands.get(task_id)
        if demand is not None and total > demand.remaining_minutes + prefs.min_block_minutes:
            violations.append(f"{task_id}: {total} min planned, more than requested")
    if result.status is PlanStatus.COMPLETE:
        for demand in problem.demands:
            if planned[demand.task_id] < demand.remaining_minutes:
                violations.append(f"{demand.task_id}: status complete but effort is missing")
    for day, total in daily.items():
        if daily_new[day] and total > prefs.max_daily_study_minutes:
            violations.append(f"{day}: {total} min of study exceeds the daily cap")
    for (subject, day), count in per_subject_day.items():
        # The preferred count is soft (solver penalty); only the absolute bound is a violation.
        if count > prefs.max_blocks_per_subject_per_day + EXTRA_BLOCKS_PER_SUBJECT:
            violations.append(f"{day}: {count} blocks of {subject} exceed the daily maximum")
    return violations


def busy_conflicts(problem: PlanningProblem) -> list[str]:
    """Locked blocks that collide with fixed commitments: protected changes needing approval."""
    return [
        f"locked block conflicts with a fixed commitment: {_label(locked)}"
        for locked in problem.locked_blocks
        if any(b.overlaps(locked.interval) for b in problem.busy)
    ]
