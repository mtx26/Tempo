"""CP-SAT study planner.

Time is measured in integer minutes from local midnight of the horizon's first day (UTC
arithmetic, so DST days last 23 h / 25 h). Fixed intervals are rounded conservatively: busy
time is widened (start floored, end ceiled) and availability narrowed, so a plan can never
overlap a commitment because of seconds in the source data.

Model:
- one family of optional study intervals per (task, local day), ordered to break symmetry;
  starts are restricted to the 15-minute grid inside free time of that day;
- NoOverlap over study intervals and the union of busy, locked and unavailable time;
- per task: planned + unplanned - overshoot == effort still to plan (remaining minus locked);
- per local day: new study + locked study <= daily cap;
- per subject and day: at most ``max_blocks_per_subject_per_day`` blocks; consecutive blocks
  of the same subject (any task) closer than the minimum gap are penalised;
- hard targets: no block ends after the deadline; soft targets: lateness is penalised.

Objective weights: unplanned effort >> soft lateness > same-subject adjacency >
non-preferred block length > overshoot.
"""

import itertools
import math
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ortools.sat.python import cp_model

from tempo.domain.models import TargetKind
from tempo.scheduler.model import (
    PlannedBlock,
    PlanningProblem,
    PlanResult,
    PlanStatus,
    StudyDemand,
    utc,
)
from tempo.scheduler.validate import busy_conflicts

ADJACENCY_PENALTY = 120
SHORT_BLOCK_PENALTY = 30
OVERSHOOT_PENALTY_PER_MINUTE = 2

type Span = tuple[int, int]


@dataclass(slots=True)
class _Block:
    demand: StudyDemand
    day: int
    present: cp_model.IntVar
    start: cp_model.LinearExpr
    end: cp_model.IntVar
    effective: cp_model.IntVar


class _Clock:
    """Converts between aware datetimes and integer minutes from a UTC origin."""

    def __init__(self, origin: datetime, timezone: ZoneInfo) -> None:
        self.origin = utc(origin)
        self.timezone = timezone

    def _seconds(self, value: datetime) -> float:
        return (utc(value) - self.origin).total_seconds()

    def floor(self, value: datetime) -> int:
        return math.floor(self._seconds(value) / 60)

    def ceil(self, value: datetime) -> int:
        return math.ceil(self._seconds(value) / 60)

    def to_datetime(self, minutes: int) -> datetime:
        return (self.origin + timedelta(minutes=minutes)).astimezone(self.timezone)


def _merge(spans: list[Span]) -> list[Span]:
    merged: list[Span] = []
    for start, end in sorted(s for s in spans if s[1] > s[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract(spans: list[Span], removed: list[Span]) -> list[Span]:
    """``spans`` minus ``removed``; ``removed`` must be sorted and non-overlapping."""
    result: list[Span] = []
    for start, end in spans:
        cursor = start
        for r_start, r_end in removed:
            if r_end <= cursor or r_start >= end:
                continue
            if r_start > cursor:
                result.append((cursor, r_start))
            cursor = max(cursor, r_end)
        if cursor < end:
            result.append((cursor, end))
    return result


def _local_days(problem: PlanningProblem, clock: _Clock) -> list[Span]:
    tz = problem.timezone
    day = problem.horizon.start.astimezone(tz).date()
    last = problem.horizon.end.astimezone(tz).date()
    days: list[Span] = []
    while day <= last:
        start = clock.floor(datetime.combine(day, time(0), tzinfo=tz))
        end = clock.floor(datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz))
        days.append((start, end))
        day += timedelta(days=1)
    return days


def _start_domain(free: list[Span], g: int, min_block: int) -> list[list[int]]:
    """Grid slots (start // g) at which a minimum-length block fits in free time."""
    ranges: list[list[int]] = []
    for start, end in free:
        first, last = math.ceil(start / g), (end - min_block) // g
        if last >= first:
            ranges.append([first, last])
    return ranges


class _Builder:
    def __init__(self, problem: PlanningProblem) -> None:
        self.problem = problem
        self.prefs = problem.preferences
        tz = problem.timezone
        first_day = problem.horizon.start.astimezone(tz).date()
        self.clock = _Clock(datetime.combine(first_day, time(0), tzinfo=tz), tz)
        self.horizon: Span = (
            self.clock.ceil(problem.horizon.start),
            self.clock.floor(problem.horizon.end),
        )
        self.model = cp_model.CpModel()
        self.blocks: list[_Block] = []
        self.intervals: list[cp_model.IntervalVar] = []
        self.penalties: list[cp_model.LinearExprT] = []
        self.unplanned: dict[str, cp_model.IntVar] = {}
        self.to_plan: dict[str, int] = {}

    def _fixed_spans(self) -> tuple[list[Span], list[Span]]:
        """Return (free study time, blocked time) inside the horizon."""
        clock, (h_start, h_end) = self.clock, self.horizon
        allowed = _merge(
            [
                (max(h_start, clock.ceil(w.start)), min(h_end, clock.floor(w.end)))
                for w in self.problem.availability
            ]
        )
        busy = _merge(
            [(clock.floor(b.start), clock.ceil(b.end)) for b in self.problem.busy]
            + [(clock.floor(b.start), clock.ceil(b.end)) for b in self.problem.locked_blocks]
        )
        free = _subtract(allowed, busy)
        blocked = _subtract([(h_start, h_end)], free)
        return free, blocked

    def _locked_minutes_by_day(self, days: list[Span]) -> dict[int, int]:
        totals: dict[int, int] = {}
        for block in self.problem.locked_blocks:
            start = self.clock.floor(block.start)
            for index, (d_start, d_end) in enumerate(days):
                if d_start <= start < d_end:
                    minutes = int(block.duration.total_seconds() // 60)
                    totals[index] = totals.get(index, 0) + minutes
        return totals

    def _add_family(
        self, demand: StudyDemand, day_index: int, free: list[Span], late_bound: int
    ) -> None:
        model, prefs, g = self.model, self.prefs, self.prefs.granularity_minutes
        target = self.clock.floor(demand.target)
        domain = _start_domain(free, g, prefs.min_block_minutes)
        capacity = sum(e - s for s, e in free)
        count = min(prefs.max_blocks_per_subject_per_day, capacity // prefs.min_block_minutes)
        if not domain or count == 0:
            return
        previous: _Block | None = None
        for k in range(count):
            tag = f"{demand.task_id}_{day_index}_{k}"
            slot = model.new_int_var_from_domain(
                cp_model.Domain.from_intervals(domain), f"slot_{tag}"
            )
            size_slots = model.new_int_var(
                prefs.min_block_minutes // g, prefs.max_block_minutes // g, f"size_{tag}"
            )
            present = model.new_bool_var(f"present_{tag}")
            start = g * slot
            size = g * size_slots
            end = model.new_int_var(
                domain[0][0] * g, domain[-1][1] * g + prefs.max_block_minutes, f"end_{tag}"
            )
            model.add(end == start + size)
            self.intervals.append(
                model.new_optional_interval_var(start, size, end, present, f"iv_{tag}")
            )
            if demand.target_kind is TargetKind.HARD:
                model.add(end <= target).only_enforce_if(present)
            else:
                late = model.new_int_var(0, late_bound, f"late_{tag}")
                model.add(late >= end - target).only_enforce_if(present)
                self.penalties.append(late)

            effective = model.new_int_var(0, prefs.max_block_minutes, f"eff_{tag}")
            model.add(effective == size).only_enforce_if(present)
            model.add(effective == 0).only_enforce_if(~present)

            short = model.new_bool_var(f"short_{tag}")
            preferred = prefs.preferred_block_minutes // g
            model.add(size_slots == preferred).only_enforce_if([present, ~short])
            self.penalties.append(SHORT_BLOCK_PENALTY * short)

            if previous is not None:
                model.add_implication(present, previous.present)
                model.add(start >= previous.end).only_enforce_if(present)
                self._penalise_adjacency(previous, start, end, present, tag, ordered=True)
            current = _Block(demand, day_index, present, start, end, effective)
            self.blocks.append(current)
            previous = current

    def _penalise_adjacency(
        self,
        other: _Block,
        start: cp_model.LinearExpr,
        end: cp_model.IntVar,
        present: cp_model.IntVar,
        tag: str,
        *,
        ordered: bool,
    ) -> None:
        model, gap = self.model, self.prefs.min_gap_same_subject_minutes
        suffix = f"{tag}_{len(self.penalties)}"
        adjacent = model.new_bool_var(f"adjacent_{suffix}")
        if ordered:
            model.add(start >= other.end + gap).only_enforce_if([present, ~adjacent])
        else:
            after = model.new_bool_var(f"after_{suffix}")
            both = [present, other.present, ~adjacent]
            model.add(start >= other.end + gap).only_enforce_if([*both, after])
            model.add(other.start >= end + gap).only_enforce_if([*both, ~after])
        self.penalties.append(ADJACENCY_PENALTY * adjacent)

    def build(self) -> None:
        prefs, model = self.prefs, self.model
        g = prefs.granularity_minutes
        free, blocked = self._fixed_spans()
        days = _local_days(self.problem, self.clock)
        self.intervals = [
            model.new_fixed_size_interval_var(s, e - s, f"blocked_{i}")
            for i, (s, e) in enumerate(blocked)
        ]

        locked_minutes: dict[str, int] = {}
        for block in self.problem.locked_blocks:
            minutes = int(block.duration.total_seconds() // 60)
            locked_minutes[block.task_id] = locked_minutes.get(block.task_id, 0) + minutes

        targets = [self.clock.floor(d.target) for d in self.problem.demands]
        late_bound = max([0, *(self.horizon[1] + prefs.max_block_minutes - t for t in targets)])
        # Per minute, leaving effort unplanned must cost more than any combination of soft
        # penalties a block can incur (lateness, adjacency with neighbours, short, overshoot).
        unplanned_weight = (
            late_bound
            + 4 * ADJACENCY_PENALTY
            + SHORT_BLOCK_PENALTY
            + OVERSHOOT_PENALTY_PER_MINUTE * prefs.min_block_minutes
            + 1
        )

        for demand in self.problem.demands:
            need = max(0, demand.remaining_minutes - locked_minutes.get(demand.task_id, 0))
            need = math.ceil(need / g) * g
            self.to_plan[demand.task_id] = need
            target = self.clock.floor(demand.target)
            if need:
                for index, (d_start, d_end) in enumerate(days):
                    if demand.target_kind is TargetKind.HARD and d_start >= target:
                        continue
                    day_free = _subtract(free, [(-(10**9), d_start), (d_end, 10**9)])
                    self._add_family(demand, index, day_free, late_bound)
            effective = [b.effective for b in self.blocks if b.demand is demand]
            unplanned = model.new_int_var(0, need, f"unplanned_{demand.task_id}")
            overshoot = model.new_int_var(0, prefs.min_block_minutes, f"over_{demand.task_id}")
            model.add(sum(effective) + unplanned - overshoot == need)
            self.unplanned[demand.task_id] = unplanned
            self.penalties.append(unplanned_weight * unplanned)
            self.penalties.append(OVERSHOOT_PENALTY_PER_MINUTE * overshoot)

        self._add_daily_constraints(days)
        model.add_no_overlap(self.intervals)
        model.minimize(sum(self.penalties))

    def _add_daily_constraints(self, days: list[Span]) -> None:
        prefs, model = self.prefs, self.model
        locked_by_day = self._locked_minutes_by_day(days)
        by_day: dict[int, list[_Block]] = {}
        for block in self.blocks:
            by_day.setdefault(block.day, []).append(block)
        for day_index, day_blocks in by_day.items():
            cap = max(0, prefs.max_daily_study_minutes - locked_by_day.get(day_index, 0))
            model.add(sum(b.effective for b in day_blocks) <= cap)
            by_subject: dict[str, list[_Block]] = {}
            for block in day_blocks:
                by_subject.setdefault(block.demand.subject, []).append(block)
            for subject_blocks in by_subject.values():
                limit = prefs.max_blocks_per_subject_per_day
                model.add(sum(b.present for b in subject_blocks) <= limit)
                for a, b in itertools.combinations(subject_blocks, 2):
                    if a.demand is not b.demand:
                        self._penalise_adjacency(a, b.start, b.end, b.present, "x", ordered=False)


def solve(
    problem: PlanningProblem,
    *,
    time_limit_seconds: float = 10.0,
    workers: int = 8,
) -> PlanResult:
    builder = _Builder(problem)
    builder.build()
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_workers = workers
    status = solver.solve(builder.model)
    status_name = solver.status_name(status)
    conflicts = busy_conflicts(problem)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return PlanResult(
            status=PlanStatus.NO_SOLUTION,
            blocks=list(problem.locked_blocks),
            unplanned_minutes={t: m for t, m in builder.to_plan.items() if m},
            conflicts=conflicts,
            solver_status=status_name,
        )

    clock = builder.clock
    planned = [
        PlannedBlock(
            task_id=b.demand.task_id,
            subject=b.demand.subject,
            start=clock.to_datetime(solver.value(b.start)),
            end=clock.to_datetime(solver.value(b.end)),
        )
        for b in builder.blocks
        if solver.boolean_value(b.present)
    ]
    unplanned_minutes = {
        t: solver.value(v) for t, v in builder.unplanned.items() if solver.value(v) > 0
    }
    demands = {d.task_id: d for d in problem.demands}
    late_minutes: dict[str, int] = {}
    for block in planned:
        demand = demands[block.task_id]
        if demand.target_kind is TargetKind.SOFT and utc(block.end) > utc(demand.target):
            minutes = int(block.duration.total_seconds() // 60)
            late_minutes[block.task_id] = late_minutes.get(block.task_id, 0) + minutes
    misses = [t for t in unplanned_minutes if demands[t].target_kind is TargetKind.HARD]
    all_blocks = sorted([*problem.locked_blocks, *planned], key=lambda b: b.start.astimezone(UTC))
    return PlanResult(
        status=PlanStatus.PARTIAL if unplanned_minutes else PlanStatus.COMPLETE,
        blocks=all_blocks,
        unplanned_minutes=unplanned_minutes,
        late_minutes=late_minutes,
        hard_deadline_misses=misses,
        conflicts=conflicts,
        solver_status=status_name,
    )
