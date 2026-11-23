"""Placement of flexible training sessions (docs/REQUIREMENTS.md: trainings are flexible, races
are fixed).

A training keeps its prescribed duration and content; only its day/time moves, inside its window
``[earliest_start, latest_end]`` and inside daily activity hours. Travel to and from its place is
part of the reserved time (``travel_before`` / ``travel_after``), so a placed session never
overlaps a commitment once travel is counted. Races and locked trainings are NOT placed here:
the caller passes them as ``busy``.

Objective: place every session (missing one costs far more than anything else), then stay close
to the previous placement (stability), then to the preferred start.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from ortools.sat.python import cp_model

from tempo.scheduler.model import Interval, utc

GRID_MINUTES = 15
MISSING_PENALTY = 10_000_000
STABILITY_WEIGHT = 4


@dataclass(frozen=True, slots=True)
class FlexibleActivity:
    activity_id: str
    title: str
    duration: timedelta
    earliest_start: datetime
    latest_end: datetime
    preferred_start: datetime | None = None
    previous_start: datetime | None = None
    travel_before: timedelta = timedelta(0)
    travel_after: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        if self.duration <= timedelta(0):
            raise ValueError("activity duration must be positive")
        if utc(self.latest_end) <= utc(self.earliest_start):
            raise ValueError("activity window must not be empty")
        for value in (self.preferred_start, self.previous_start):
            if value is not None:
                utc(value)


@dataclass(frozen=True, slots=True)
class PlacedActivity:
    activity_id: str
    start: datetime
    end: datetime
    reserved: Interval
    """Session plus travel before/after: what study planning must treat as busy."""


@dataclass(slots=True)
class ActivityPlan:
    placed: list[PlacedActivity] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)
    """Activity ids for which no slot exists in the window (reported, never forced)."""
    solver_status: str = ""


def _minutes(origin: datetime, value: datetime) -> float:
    return (utc(value) - utc(origin)).total_seconds() / 60


def plan_activities(
    activities: list[FlexibleActivity],
    busy: list[Interval],
    timezone: ZoneInfo,
    *,
    day_start: time = time(6, 0),
    day_end: time = time(22, 0),
    time_limit_seconds: float = 5.0,
) -> ActivityPlan:
    if not activities:
        return ActivityPlan(solver_status="EMPTY")
    origin = min(utc(a.earliest_start) for a in activities) - timedelta(days=1)
    horizon_end = max(utc(a.latest_end) for a in activities) + timedelta(days=1)
    model = cp_model.CpModel()
    intervals: list[cp_model.IntervalVar] = []
    for index, commitment in enumerate(busy):
        s, e = (
            math.floor(_minutes(origin, commitment.start)),
            math.ceil(_minutes(origin, commitment.end)),
        )
        if e > s:
            intervals.append(model.new_fixed_size_interval_var(s, e - s, f"busy_{index}"))

    # Outside activity hours (session itself must be within [day_start, day_end] local time).
    day = (origin.astimezone(timezone)).date()
    night_spans: list[tuple[int, int]] = []
    while datetime.combine(day, time(0), tzinfo=timezone) < horizon_end:
        opens = datetime.combine(day, day_start, tzinfo=timezone)
        closes = datetime.combine(day, day_end, tzinfo=timezone)
        nxt = datetime.combine(day + timedelta(days=1), day_start, tzinfo=timezone)
        night_spans.append((math.ceil(_minutes(origin, closes)), math.floor(_minutes(origin, nxt))))
        if day == origin.astimezone(timezone).date():
            night_spans.append((0, math.floor(_minutes(origin, opens))))
        day += timedelta(days=1)

    penalties: list[cp_model.LinearExprT] = []
    entries: list[tuple[FlexibleActivity, cp_model.IntVar, cp_model.IntVar]] = []
    for index, item in enumerate(activities):
        duration = math.ceil(item.duration.total_seconds() / 60)
        before = math.ceil(item.travel_before.total_seconds() / 60)
        after = math.ceil(item.travel_after.total_seconds() / 60)
        first = math.ceil(_minutes(origin, item.earliest_start) / GRID_MINUTES)
        last = math.floor((_minutes(origin, item.latest_end) - duration) / GRID_MINUTES)
        present = model.new_bool_var(f"present_{index}")
        if last < first:
            model.add(present == 0)
            last = first
        slot = model.new_int_var(first, last, f"slot_{index}")
        start = GRID_MINUTES * slot
        session = model.new_optional_fixed_size_interval_var(
            start, duration, present, f"session_{index}"
        )
        reserved = model.new_optional_fixed_size_interval_var(
            start - before, before + duration + after, present, f"reserved_{index}"
        )
        intervals.append(reserved)
        window_start, window_end = GRID_MINUTES * first, GRID_MINUTES * last + duration
        for night_index, (n_start, n_end) in enumerate(night_spans):
            if n_end > n_start and n_start < window_end and n_end > window_start:
                night = model.new_fixed_size_interval_var(
                    n_start, n_end - n_start, f"night_{index}_{night_index}"
                )
                model.add_no_overlap([session, night])
        penalties.append(MISSING_PENALTY * (1 - present))
        for weight, anchor in ((STABILITY_WEIGHT, item.previous_start), (1, item.preferred_start)):
            if anchor is not None:
                target = round(_minutes(origin, anchor))
                distance = model.new_int_var(0, 10**7, f"dist_{index}_{weight}")
                model.add_abs_equality(distance, start - target)
                penalties.append(weight * distance)
        entries.append((item, present, slot))

    model.add_no_overlap(intervals)
    model.minimize(sum(penalties))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_workers = 8
    status = solver.solve(model)
    result = ActivityPlan(solver_status=solver.status_name(status))
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        result.unplaced = [a.activity_id for a in activities]
        return result
    for item, present, slot in entries:
        if not solver.boolean_value(present):
            result.unplaced.append(item.activity_id)
            continue
        begins = (origin + timedelta(minutes=GRID_MINUTES * solver.value(slot))).astimezone(
            timezone
        )
        ends = begins + item.duration
        result.placed.append(
            PlacedActivity(
                item.activity_id,
                begins,
                ends,
                Interval(begins - item.travel_before, ends + item.travel_after),
            )
        )
    return result
