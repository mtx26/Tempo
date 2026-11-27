"""The single CP-SAT planner: study blocks AND flexible activities in ONE model.

No category is planned before another: study, trainings, personal outings and task sessions
compete for the same free time only through their deadlines, hard constraints and individual
priorities (ARCHITECTURE.md §8).

Time is measured in integer minutes from local midnight of the horizon's first day (UTC
arithmetic: DST days last 23 h / 25 h). Fixed intervals are rounded conservatively (busy time
widened, availability narrowed).

Hard rules (never relaxed): busy time (commitments, journeys, uncertain periods), fixed blocks,
study availability, activity windows and allowed intervals (category windows, unavailability,
presence at the residence), deadlines of every kind (no work after a deadline without the user's
approval), the coach's order, explicit recovery days, block length bounds and the 15-min grid.

Soft preferences (objective, weights below): placing work beats every preference; then
activity hours, darkness for outdoor trainings, trainings between classes, class days, time of
day, the coach's suggested day, stability (decreasing with the distance in time), regular
distribution until the deadline, subject alternation, breaks, preferred block length.
"""

import itertools
import math
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from ortools.sat.python import cp_model

from tempo.const import (
    BETWEEN_CLASSES_COST,
    CLASS_DAY_COST,
    COACH_DAY_COST,
    DARK_COST,
    DISTRIBUTION_PER_MINUTE,
    GRID_MINUTES,
    MAX_BLOCKS_PER_TASK_DAY,
    MISSING_BREAK_COST,
    NO_DEADLINE_PER_MINUTE,
    OUTSIDE_HOURS_COST,
    OVERSHOOT_PER_MINUTE,
    PREFERRED_START_COST,
    PREFERRED_START_PER_MINUTE,
    SAME_SUBJECT_COST,
    SHORT_BLOCK_COST,
    STABILITY_COST,
    TIME_OF_DAY_FAR_COST,
    TIME_OF_DAY_STEP_COST,
    TIMES_OF_DAY,
    UNPLANNED_PER_MINUTE,
)
from tempo.domain.models import TargetKind
from tempo.scheduler.model import (
    ClassSlot,
    FlexibleActivity,
    PlacedActivity,
    PlannedBlock,
    PlanningProblem,
    PlanResult,
    PlanStatus,
    StudyDemand,
    study_effort,
)
from tempo.timeutil import Interval, utc

# --- objective weights ---------------------------------------------------------------------
# Leaving work unplanned dominates every preference an item can accumulate (checked in
# tests/test_scheduler_weights.py), so a preference never makes something unplanned.

type Span = tuple[int, int]


def normalise(text: str) -> set[str]:
    """Significant words, lower case without accents (subject matching)."""
    plain = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c if c.isalnum() else " " for c in plain if not unicodedata.combining(c))
    return {w for w in plain.split() if len(w) >= 4}


def same_subject(subject: str, title: str) -> bool:
    return bool(normalise(subject) & normalise(title))


def stability_weight(days_ahead: int) -> int:
    return STABILITY_COST // (max(days_ahead, 0) + 1)


@dataclass(slots=True)
class _Block:
    demand: StudyDemand
    day: int
    present: cp_model.IntVar
    start: cp_model.LinearExpr
    end: cp_model.IntVar
    effective: cp_model.IntVar
    slot: cp_model.IntVar
    size: cp_model.IntVar


@dataclass(slots=True)
class _Activity:
    item: FlexibleActivity
    present: cp_model.IntVar
    index: cp_model.IntVar
    start: cp_model.IntVar
    starts: list[int]
    days: list[int]


class _Clock:
    def __init__(self, origin: datetime, timezone: object) -> None:
        self.origin = utc(origin)
        self.timezone = timezone

    def floor(self, value: datetime) -> int:
        return math.floor((utc(value) - self.origin).total_seconds() / 60)

    def ceil(self, value: datetime) -> int:
        return math.ceil((utc(value) - self.origin).total_seconds() / 60)

    def to_datetime(self, minutes: int) -> datetime:
        return (self.origin + timedelta(minutes=minutes)).astimezone(self.timezone)  # type: ignore[arg-type]


def _merge(spans: list[Span]) -> list[Span]:
    merged: list[Span] = []
    for start, end in sorted(s for s in spans if s[1] > s[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract(spans: list[Span], removed: list[Span]) -> list[Span]:
    """``spans`` minus ``removed`` (``removed`` sorted and non-overlapping)."""
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


class _Builder:
    def __init__(self, problem: PlanningProblem) -> None:
        self.p = problem
        self.prefs = problem.preferences
        tz = problem.timezone
        self.first_day = problem.horizon.start.astimezone(tz).date()
        self.clock = _Clock(datetime.combine(self.first_day, time(0), tzinfo=tz), tz)
        self.h_start = self.clock.ceil(problem.horizon.start)
        self.h_end = self.clock.floor(problem.horizon.end)
        self.model = cp_model.CpModel()
        self.penalties: list[cp_model.LinearExprT] = []
        self.blocks: list[_Block] = []
        self.activities: list[_Activity] = []
        self.to_plan: dict[str, int] = {}
        self.global_intervals: list[cp_model.IntervalVar] = []
        self.study_gaps: dict[int, list[cp_model.IntervalVar]] = {}
        self.break_intervals: list[cp_model.IntervalVar] = []
        self.subject_intervals: dict[str, list[cp_model.IntervalVar]] = {}
        last = problem.horizon.end.astimezone(tz).date()
        self.days: list[Span] = []
        day = self.first_day
        while day <= last:
            self.days.append(
                (
                    self.clock.floor(datetime.combine(day, time(0), tzinfo=tz)),
                    self.clock.floor(datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)),
                )
            )
            day += timedelta(days=1)

    # -- helpers ----------------------------------------------------------------------------
    def _day_of(self, minute: int) -> int:
        return (self.clock.to_datetime(minute).date() - self.first_day).days

    def _local(self, minute: int) -> datetime:
        return self.clock.to_datetime(minute)

    def _busy(self) -> list[Span]:
        return _merge(
            [(self.clock.floor(b.start), self.clock.ceil(b.end)) for b in self.p.busy]
            + [(self.clock.floor(b.start), self.clock.ceil(b.end)) for b in self.p.fixed_blocks]
        )

    def _extended(
        self,
        start: cp_model.LinearExpr,
        end: cp_model.IntVar,
        extra: int,
        kept: cp_model.IntVar,
        present: cp_model.IntVar,
        tag: str,
    ) -> cp_model.IntervalVar:
        """The block followed by ``extra`` minutes when ``kept`` (a soft margin)."""
        stretched = self.model.new_int_var(0, 10**6, f"x_{tag}")
        self.model.add(stretched == end + extra * kept)
        length = self.model.new_int_var(0, 24 * 60, f"l_{tag}")
        self.model.add(length == stretched - start)
        return self.model.new_optional_interval_var(start, length, stretched, present, tag)

    def _zone(self, start: int, length: int, cost: int, tag: str) -> cp_model.IntervalVar:
        """Optional fixed interval kept by default; dropping it (to let a block in) costs."""
        on = self.model.new_bool_var(f"zone_{tag}")
        self.penalties.append(cost * (1 - on))
        return self.model.new_optional_fixed_size_interval_var(start, length, on, f"z_{tag}")

    # -- build --------------------------------------------------------------------------------
    def build(self) -> None:
        busy = self._busy()
        for i, (s, e) in enumerate(busy):
            self.global_intervals.append(self.model.new_fixed_size_interval_var(s, e - s, f"b{i}"))
        allowed = _merge(
            [
                (
                    max(self.h_start, self.clock.ceil(w.start)),
                    min(self.h_end, self.clock.floor(w.end)),
                )
                for w in self.p.availability
            ]
        )
        self.study_free = _subtract(allowed, busy)
        # Busy time is already excluded by the global constraint. Only the internal
        # gaps of study availability need their own constraint, shared across tasks.
        for index, (left, right) in enumerate(itertools.pairwise(allowed)):
            start, end = left[1], right[0]
            day = self._day_of(start)
            if self._day_of(end) == day:
                self.study_gaps.setdefault(day, []).append(
                    self.model.new_fixed_size_interval_var(start, end - start, f"study_gap_{index}")
                )
        for demand in self.p.demands:
            self._add_demand(demand)
        self._add_fixed_block_zones()
        self._add_class_zones()
        for index, item in enumerate(self.p.activities):
            self._add_activity(index, item)
        self._add_sequences_and_rest()
        self._add_stability()
        model = self.model
        model.add_no_overlap(self.global_intervals)
        for intervals in self.study_gaps.values():
            model.add_no_overlap(intervals)
        model.add_no_overlap(self.break_intervals)
        for intervals in self.subject_intervals.values():
            model.add_no_overlap(intervals)
        model.minimize(sum(self.penalties))

    def _add_demand(self, demand: StudyDemand) -> None:
        prefs, g, model = self.prefs, self.prefs.granularity_minutes, self.model
        locked = sum(
            int(b.duration.total_seconds() // 60)
            for b in self.p.fixed_blocks
            if b.task_id == demand.task_id
        )
        need = max(0, demand.remaining_minutes - locked)
        need = math.ceil(need / g) * g
        self.to_plan[demand.task_id] = need
        target = None if demand.target is None else self.clock.floor(demand.target)
        not_before = (
            self.h_start if demand.not_before is None else self.clock.ceil(demand.not_before)
        )
        families: list[tuple[int, list[Span]]] = []
        for index, (d_start, d_end) in enumerate(self.days):
            low, high = max(d_start, not_before), d_end if target is None else min(d_end, target)
            if high - low < prefs.min_block_minutes:
                continue
            free = _subtract(self.study_free, [(-(10**9), low), (high, 10**9)])
            if sum(e - s for s, e in free) >= prefs.min_block_minutes:
                families.append((index, free))
        # Regular share per day, never below one preferred session (90-min sessions first).
        share = max(math.ceil(need / max(1, len(families)) / g) * g, prefs.preferred_block_minutes)
        mine: list[_Block] = []
        for index, free in families:
            family = self._add_family(demand, index, free, need)
            mine += family
            if family and share < need:
                load = sum(b.effective for b in family)
                excess = model.new_int_var(0, 24 * 60, f"excess_{demand.task_id}_{index}")
                model.add(excess >= load - share)
                self.penalties.append(DISTRIBUTION_PER_MINUTE * excess)
        unplanned = model.new_int_var(0, need, f"unplanned_{demand.task_id}")
        overshoot = model.new_int_var(0, prefs.min_block_minutes, f"over_{demand.task_id}")
        model.add(sum(b.effective for b in mine) + unplanned - overshoot == need)
        weight = NO_DEADLINE_PER_MINUTE if demand.target is None else UNPLANNED_PER_MINUTE
        self.penalties.append(weight * demand.priority * unplanned)
        self.penalties.append(OVERSHOOT_PER_MINUTE * overshoot)

    def _add_family(
        self, demand: StudyDemand, day: int, free: list[Span], need: int
    ) -> list[_Block]:
        prefs, g, model = self.prefs, self.prefs.granularity_minutes, self.model
        windows = [
            (s, e) for s, e in free if (e - prefs.min_block_minutes) // g >= math.ceil(s / g)
        ]
        capacity = sum(e - s for s, e in free)
        count = min(
            MAX_BLOCKS_PER_TASK_DAY,
            math.ceil(min(need, capacity) / prefs.preferred_block_minutes) + 1,
            capacity // prefs.min_block_minutes,
        )
        if not windows or count <= 0:
            return []
        domain = cp_model.Domain.from_intervals(
            [[math.ceil(s / g), (e - prefs.min_block_minutes) // g] for s, e in windows]
        )
        latest = windows[-1][1]
        family: list[_Block] = []
        for k in range(count):
            tag = f"{demand.task_id}_{day}_{k}"
            slot = model.new_int_var_from_domain(domain, f"s_{tag}")
            size = model.new_int_var(
                prefs.min_block_minutes // g, prefs.max_block_minutes // g, f"n_{tag}"
            )
            present = model.new_bool_var(f"p_{tag}")
            start = g * slot
            end = model.new_int_var(windows[0][0], latest, f"e_{tag}")
            model.add(end == start + g * size)
            interval = model.new_optional_interval_var(start, g * size, end, present, f"iv_{tag}")
            self.global_intervals.append(interval)
            if day in self.study_gaps:
                self.study_gaps[day].append(interval)
            effective = model.new_int_var(0, prefs.max_block_minutes, f"eff_{tag}")
            model.add(effective == g * size).only_enforce_if(present)
            model.add(effective == 0).only_enforce_if(~present)
            short = model.new_bool_var(f"short_{tag}")
            model.add(size == prefs.preferred_block_minutes // g).only_enforce_if([present, ~short])
            self.penalties.append(SHORT_BLOCK_COST * short)
            # Preferred break after the session (soft): the session plus its break must not
            # overlap another study session; dropping the break costs.
            rest = model.new_bool_var(f"break_{tag}")
            model.add_implication(rest, present)
            self.penalties.append(MISSING_BREAK_COST * (present - rest))
            self.break_intervals.append(
                self._extended(start, end, prefs.break_minutes, rest, present, f"bk_{tag}")
            )
            # Subject alternation (soft): same subject again within the gap costs.
            alternate = model.new_bool_var(f"alt_{tag}")
            model.add_implication(alternate, present)
            self.penalties.append(SAME_SUBJECT_COST * (present - alternate))
            gap = prefs.min_gap_same_subject_minutes
            self.subject_intervals.setdefault(demand.subject, []).append(
                self._extended(start, end, gap, alternate, present, f"sj_{tag}")
            )
            if family:
                model.add_implication(present, family[-1].present)
                model.add(start >= family[-1].end).only_enforce_if(present)
            block = _Block(demand, day, present, start, end, effective, slot, size)
            family.append(block)
            self.blocks.append(block)
        return family

    def _add_fixed_block_zones(self) -> None:
        """Breaks and alternation also count after fixed blocks (soft, like the others)."""
        prefs = self.prefs
        subjects = {d.task_id: d.subject for d in self.p.demands}
        for i, block in enumerate(self.p.fixed_blocks):
            end = self.clock.ceil(block.end)
            if prefs.break_minutes:
                self.break_intervals.append(
                    self._zone(end, prefs.break_minutes, MISSING_BREAK_COST, f"fb{i}")
                )
            subject = subjects.get(block.task_id, block.subject)
            if prefs.min_gap_same_subject_minutes and subject in self.subject_intervals:
                self.subject_intervals[subject].append(
                    self._zone(end, prefs.min_gap_same_subject_minutes, SAME_SUBJECT_COST, f"fs{i}")
                )

    def _add_class_zones(self) -> None:
        """No study of a subject right after its class when alternatives exist (soft)."""
        gap = self.prefs.min_gap_same_subject_minutes
        if not gap:
            return
        for i, slot in enumerate(self.p.classes):
            end = self.clock.ceil(slot.end)
            if not self.h_start - gap <= end <= self.h_end:
                continue
            for subject, intervals in self.subject_intervals.items():
                if same_subject(subject, slot.title):
                    intervals.append(
                        self._zone(end, gap, SAME_SUBJECT_COST, f"c{i}_{len(intervals)}")
                    )

    # -- activities ---------------------------------------------------------------------------
    def _slot_cost(self, item: FlexibleActivity, start: int, duration: int) -> int:
        p, tz = self.p, self.p.timezone
        begins, ends = self._local(start), self._local(start + duration)
        day = begins.date()
        cost = 0
        opens, closes = p.hours_on(day)
        if begins < datetime.combine(day, opens, tzinfo=tz) or ends > datetime.combine(
            day, closes, tzinfo=tz
        ):
            cost += OUTSIDE_HOURS_COST
        if item.training:
            prefs = p.training_at(begins)
            classes = [c for c in p.classes if c.start.astimezone(tz).date() == day]
            if classes and prefs.free_days:
                cost += CLASS_DAY_COST
            if any(utc(c.end) <= utc(begins) for c in classes) and any(
                utc(c.start) >= utc(ends) for c in classes
            ):
                cost += BETWEEN_CLASSES_COST
            if prefs.time_of_day in TIMES_OF_DAY:
                middle = (begins + (ends - begins) / 2).time()
                period = 0 if middle < time(12) else 1 if middle < time(18) else 2
                steps = abs(period - TIMES_OF_DAY.index(prefs.time_of_day))
                cost += (0, TIME_OF_DAY_STEP_COST, TIME_OF_DAY_FAR_COST)[steps]
            if prefs.daylight and item.outdoor and p.sun is not None:
                sunrise, sunset = p.sun(day)
                if utc(begins) < utc(sunrise) or utc(ends) > utc(sunset):
                    cost += DARK_COST
        if item.preferred_day is not None:
            cost += COACH_DAY_COST * abs((day - item.preferred_day).days)
        if item.preferred_start is not None:
            wanted = self.clock.floor(item.preferred_start)
            if start != wanted:
                cost += PREFERRED_START_COST + PREFERRED_START_PER_MINUTE * abs(start - wanted)
        if item.previous_start is not None:
            previous = self.clock.floor(item.previous_start)
            if start != previous:
                ahead = (item.previous_start.astimezone(tz).date() - self.first_day).days
                weight = stability_weight(ahead)
                cost += weight + max(1, weight // 240) * abs(start - previous)
        return cost

    def _rest_ok(self, item: FlexibleActivity, day: date) -> bool:
        """Explicit recovery against fixed trainings and races (days)."""
        noon = datetime.combine(day, time(12), tzinfo=self.p.timezone)
        general = self.p.training_at(noon).rest_days
        need = max(general, item.rest_days)
        for other in self.p.sport_days:
            gap = (day - other).days
            if 0 <= gap < need or (gap < 0 and -gap < general):
                return False
        return True

    def _add_activity(self, index: int, item: FlexibleActivity) -> None:
        model, g = self.model, GRID_MINUTES
        duration = math.ceil(item.duration.total_seconds() / 60)
        before = math.ceil(item.travel_before.total_seconds() / 60)
        after = math.ceil(item.travel_after.total_seconds() / 60)
        low = max(self.clock.ceil(item.earliest_start), self.h_start + before)
        high = self.clock.floor(item.latest_end) - duration
        windows = (
            None
            if item.allowed is None
            else [(self.clock.ceil(w.start), self.clock.floor(w.end)) for w in item.allowed]
        )
        starts: list[int] = []
        for slot in range(math.ceil(low / g), high // g + 1):
            start = slot * g
            if windows is not None and not any(
                s <= start and start + duration <= e for s, e in windows
            ):
                continue
            if item.training and not self._rest_ok(item, self._local(start).date()):
                continue
            starts.append(start)
        present = model.new_bool_var(f"a_present_{index}")
        weight = UNPLANNED_PER_MINUTE if item.deadline else NO_DEADLINE_PER_MINUTE
        self.penalties.append(weight * item.priority * duration * (1 - present))
        if not starts:
            model.add(present == 0)
            starts = [max(low, 0) // g * g]
        position = model.new_int_var(0, len(starts) - 1, f"a_pos_{index}")
        start_var = model.new_int_var(min(starts), max(starts), f"a_start_{index}")
        model.add_element(position, starts, start_var)
        costs = [self._slot_cost(item, s, duration) for s in starts]
        if max(costs) > 0:
            cost = model.new_int_var(0, max(costs), f"a_cost_{index}")
            model.add_element(position, costs, cost)
            paid = model.new_int_var(0, max(costs), f"a_paid_{index}")
            model.add(paid == cost).only_enforce_if(present)
            model.add(paid == 0).only_enforce_if(~present)
            self.penalties.append(paid)
        self.global_intervals.append(
            model.new_optional_fixed_size_interval_var(
                start_var - before, before + duration + after, present, f"a_res_{index}"
            )
        )
        days = [self._day_of(s) for s in starts]
        if item.previous_start is not None:
            hint = self.clock.floor(item.previous_start)
            if hint in starts:
                model.add_hint(position, starts.index(hint))
                model.add_hint(present, 1)
        self.activities.append(_Activity(item, present, position, start_var, starts, days))

    def _add_sequences_and_rest(self) -> None:
        model = self.model
        day_vars: dict[int, cp_model.IntVar] = {}

        def day_of(i: int) -> cp_model.IntVar:
            if i not in day_vars:
                act = self.activities[i]
                var = model.new_int_var(min(act.days), max(act.days), f"a_day_{i}")
                model.add_element(act.index, act.days, var)
                day_vars[i] = var
            return day_vars[i]

        def ends(i: int) -> cp_model.LinearExprT:
            act = self.activities[i]
            return act.start + math.ceil(act.item.duration.total_seconds() / 60)

        groups: dict[str, list[int]] = {}
        for i, act in enumerate(self.activities):
            if act.item.sequence_group is not None:
                groups.setdefault(act.item.sequence_group, []).append(i)
        for members in groups.values():
            members.sort(key=lambda i: self.activities[i].item.sequence_index)
            # Adjacent pairs alone lose the precedence when a middle session is absent.
            for a, b in itertools.combinations(members, 2):
                both = [self.activities[a].present, self.activities[b].present]
                model.add(self.activities[b].start >= ends(a)).only_enforce_if(both)
        trainings = [i for i, a in enumerate(self.activities) if a.item.training]
        general = self.p.training.rest_days
        for x, i in enumerate(trainings):
            for j in trainings[x + 1 :]:
                need_i = max(general, self.activities[i].item.rest_days)
                need_j = max(general, self.activities[j].item.rest_days)
                if not need_i and not need_j:
                    continue
                later = model.new_bool_var(f"later_{i}_{j}")  # i after j
                both = [self.activities[i].present, self.activities[j].present]
                model.add(day_of(i) - day_of(j) >= need_i).only_enforce_if([*both, later])
                model.add(self.activities[i].start >= ends(j)).only_enforce_if([*both, later])
                model.add(day_of(j) - day_of(i) >= need_j).only_enforce_if([*both, ~later])
                model.add(self.activities[j].start >= ends(i)).only_enforce_if([*both, ~later])

    def _add_stability(self) -> None:
        """Previous blocks: kept, shifted (cost grows with the shift) or dropped (full cost),
        the cost decreasing with the distance in time."""
        model, tz = self.model, self.p.timezone
        families: dict[tuple[str, int], list[_Block]] = {}
        for block in self.blocks:
            families.setdefault((block.demand.task_id, block.day), []).append(block)
        by_block: dict[int, list[cp_model.IntVar]] = {}
        for index, previous in enumerate(self.p.previous_blocks):
            start = self.clock.floor(previous.start)
            size = self.clock.floor(previous.end) - start
            candidates = families.get((previous.task_id, self._day_of(start)), [])
            if not candidates or size <= 0:
                continue  # cannot be kept (task done, day past...): a constant
            weight = stability_weight((previous.start.astimezone(tz).date() - self.first_day).days)
            per_minute = max(1, weight // 240)
            matches: list[cp_model.IntVar] = []
            for k, block in enumerate(candidates):
                match = model.new_bool_var(f"keep_{index}_{k}")
                model.add_implication(match, block.present)
                model.add(block.effective == size).only_enforce_if(match)
                distance = model.new_int_var(0, 2 * 24 * 60, f"dist_{index}_{k}")
                model.add_abs_equality(distance, block.start - start)
                shift = model.new_int_var(0, per_minute * 2 * 24 * 60, f"shift_{index}_{k}")
                model.add(shift >= per_minute * distance).only_enforce_if(match)
                self.penalties.append(shift)
                matches.append(match)
                by_block.setdefault(id(block), []).append(match)
            model.add_at_most_one(matches)
            self.penalties.append(weight * (1 - sum(matches)))
        for matches in by_block.values():
            if len(matches) > 1:
                model.add_at_most_one(matches)

    # -- warm start ---------------------------------------------------------------------------
    def warm_start(self) -> None:
        """Greedy hints: day by day, most urgent tasks first, regular daily shares."""
        prefs, g = self.prefs, self.prefs.granularity_minutes
        remaining = dict(self.to_plan)
        placed: list[Span] = []
        families: dict[tuple[str, int], list[_Block]] = {}
        for block in self.blocks:
            families.setdefault((block.demand.task_id, block.day), []).append(block)
        preserved: dict[tuple[str, int], list[tuple[int, int]]] = {}
        for previous in sorted(self.p.previous_blocks, key=lambda b: utc(b.start)):
            start, end = self.clock.ceil(previous.start), self.clock.floor(previous.end)
            length = end - start
            day = self._day_of(start)
            key = (previous.task_id, day)
            family = families.get(key)
            if not family or not (prefs.min_block_minutes <= length <= prefs.max_block_minutes):
                continue
            demand = family[0].demand
            hints = preserved.setdefault(key, [])
            if (
                start % g
                or length % g
                or len(hints) >= len(family)
                or remaining[previous.task_id] < length
                or end > self.days[day][1]
                or (demand.target is not None and end > self.clock.floor(demand.target))
                or (demand.not_before is not None and start < self.clock.ceil(demand.not_before))
                or not any(s <= start and end <= e for s, e in self.study_free)
                or any(start < e and s < end for s, e in placed)
            ):
                continue
            hints.append((start, length))
            placed.append((start, end + prefs.break_minutes))
            remaining[previous.task_id] -= length
        days_of: dict[str, int] = {}
        for task, _ in families:
            days_of[task] = days_of.get(task, 0) + 1
        chosen: dict[int, tuple[int, int]] = {}
        order = sorted(
            self.p.demands,
            key=lambda d: (
                d.target is None,
                utc(d.target) if d.target else datetime.max.replace(tzinfo=UTC),
            ),
        )
        for day in range(len(self.days)):
            for demand in order:
                family = families.get((demand.task_id, day), [])
                if not family:
                    continue
                share = max(
                    math.ceil(self.to_plan[demand.task_id] / days_of[demand.task_id] / g) * g,
                    prefs.preferred_block_minutes,
                )
                slots = preserved.get((demand.task_id, day), [])
                used = sum(length for _, length in slots)
                for _ in range(len(family) - len(slots)):
                    need = remaining.get(demand.task_id, 0)
                    if need <= 0 or used >= share:
                        break
                    length = max(prefs.min_block_minutes, min(prefs.preferred_block_minutes, need))
                    length = math.ceil(length / g) * g
                    d_start, d_end = self.days[day]
                    free = _subtract(self.study_free, [(-(10**9), d_start), (d_end, 10**9)])
                    if demand.not_before is not None:
                        free = _subtract(free, [(-(10**9), self.clock.ceil(demand.not_before))])
                    spot = _first_fit(free, placed, length, g)
                    if spot is None:
                        break
                    if demand.target is not None and spot + length > self.clock.floor(
                        demand.target
                    ):
                        break
                    slots.append((spot, length))
                    placed.append((spot, spot + length + prefs.break_minutes))
                    used += length
                    remaining[demand.task_id] = need - length
                for block, (spot, length) in zip(family, sorted(slots), strict=False):
                    chosen[id(block)] = (spot // g, length // g)
        for block in self.blocks:
            hint = chosen.get(id(block))
            self.model.add_hint(block.present, 1 if hint else 0)
            if hint:
                self.model.add_hint(block.slot, hint[0])
                self.model.add_hint(block.size, hint[1])


def _first_fit(free: list[Span], placed: list[Span], length: int, g: int) -> int | None:
    for start, end in _subtract(free, _merge(placed)):
        candidate = math.ceil(start / g) * g
        if candidate + length <= end:
            return candidate
    return None


def objective_lower_bound(to_plan: dict[str, int], preferred: int, minimum: int) -> int:
    """Cost no plan can avoid (every other term is >= 0): effort that is not a multiple of the
    preferred length needs a shorter block or a small overshoot."""
    bound = 0
    for need in to_plan.values():
        rest = need % preferred
        if rest == 0:
            continue
        extra = preferred - rest
        overshoot = OVERSHOOT_PER_MINUTE * extra if extra <= minimum else None
        bound += SHORT_BLOCK_COST if overshoot is None else min(SHORT_BLOCK_COST, overshoot)
    return bound


class _StopAtBound(cp_model.CpSolverSolutionCallback):
    def __init__(self, bound: int) -> None:
        super().__init__()
        self.bound = bound
        self.reached = False

    def on_solution_callback(self) -> None:
        if self.objective_value <= self.bound:
            self.reached = True
            self.stop_search()


def solve(
    problem: PlanningProblem,
    *,
    time_limit_seconds: float = 10.0,
    workers: int = 8,
    warm_start: bool = True,
) -> PlanResult:
    """Plan study blocks and flexible activities together (one CP-SAT run)."""
    builder = _Builder(problem)
    builder.build()
    if warm_start:
        builder.warm_start()
    else:
        builder.model.clear_hints()  # type: ignore[no-untyped-call]
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_workers = workers
    prefs = problem.preferences
    stopper = _StopAtBound(
        objective_lower_bound(
            builder.to_plan, prefs.preferred_block_minutes, prefs.min_block_minutes
        )
    )
    status = solver.solve(builder.model, stopper)
    status_name = solver.status_name(status)
    if stopper.reached and status == cp_model.FEASIBLE:
        status_name = "OPTIMAL_BY_BOUND"
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # Every item is optional: no answer means the time limit was hit (inconclusive).
        return PlanResult(
            status=PlanStatus.NO_SOLUTION,
            blocks=list(problem.fixed_blocks),
            unplanned_minutes={t: m for t, m in builder.to_plan.items() if m},
            solver_status=status_name,
        )
    clock = builder.clock
    planned = [
        PlannedBlock(
            b.demand.task_id,
            b.demand.subject,
            clock.to_datetime(solver.value(b.start)),
            clock.to_datetime(solver.value(b.end)),
        )
        for b in builder.blocks
        if solver.boolean_value(b.present)
    ]
    placed: list[PlacedActivity] = []
    unplaced: list[str] = []
    outside: list[str] = []
    for act in builder.activities:
        item = act.item
        if not solver.boolean_value(act.present):
            unplaced.append(item.activity_id)
            continue
        begins = clock.to_datetime(solver.value(act.start))
        ends = begins + item.duration
        placed.append(
            PlacedActivity(
                item.activity_id,
                begins,
                ends,
                Interval(begins - item.travel_before, ends + item.travel_after),
            )
        )
        opens, closes = problem.hours_on(begins.date())
        if begins < datetime.combine(begins.date(), opens, tzinfo=problem.timezone) or ends > (
            datetime.combine(begins.date(), closes, tzinfo=problem.timezone)
        ):
            outside.append(item.activity_id)
    blocks = sorted([*problem.fixed_blocks, *planned], key=lambda b: utc(b.start))
    effort = study_effort(problem.demands, blocks)
    unplanned = {task: e.unplanned_minutes for task, e in effort.items() if e.unplanned_minutes}
    misses = [
        task
        for task, e in effort.items()
        if e.before_deadline_minutes is not None and e.before_deadline_minutes < e.remaining_minutes
    ]
    hard = [
        d.task_id
        for d in problem.demands
        if d.target_kind is TargetKind.HARD and d.task_id in misses
    ]
    return PlanResult(
        status=PlanStatus.PARTIAL if unplanned or unplaced or misses else PlanStatus.COMPLETE,
        blocks=blocks,
        activities=placed,
        unplaced=unplaced,
        outside_hours=outside,
        unplanned_minutes=unplanned,
        hard_deadline_misses=hard,
        deadline_misses=misses,
        solver_status=status_name,
        objective=solver.objective_value,
        study_effort=effort,
    )


__all__ = ["ClassSlot", "objective_lower_bound", "same_subject", "solve"]
