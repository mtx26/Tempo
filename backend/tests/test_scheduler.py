"""The single CP-SAT planner: study blocks and flexible activities in ONE model.

Each test names the validated decision it checks (ARCHITECTURE.md §8)."""

import itertools
import time as clock
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tempo.const import (
    BETWEEN_CLASSES_COST,
    CLASS_DAY_COST,
    COACH_DAY_COST,
    DARK_COST,
    DISTRIBUTION_PER_MINUTE,
    MISSING_BREAK_COST,
    NO_DEADLINE_PER_MINUTE,
    OUTSIDE_HOURS_COST,
    PREFERRED_START_COST,
    SAME_SUBJECT_COST,
    SHORT_BLOCK_COST,
    STABILITY_COST,
    TIME_OF_DAY_FAR_COST,
    TIME_OF_DAY_STEP_COST,
    UNPLANNED_PER_MINUTE,
)
from tempo.domain.models import TargetKind
from tempo.scheduler.model import (
    ClassSlot,
    FlexibleActivity,
    PlannedBlock,
    PlanningProblem,
    PlanResult,
    PlanStatus,
    StudyDemand,
    TemporaryPreference,
    TrainingPreferences,
)
from tempo.scheduler.solver import objective_lower_bound, same_subject, solve, stability_weight
from tempo.scheduler.validate import validate_plan
from tempo.timeutil import Interval, utc

TZ = ZoneInfo("Europe/Brussels")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 12, day, hour, minute, tzinfo=TZ)


def windows(first: int, last: int, start: int = 8, end: int = 20) -> list[Interval]:
    return [Interval(at(d, start), at(d, end)) for d in range(first, last + 1)]


def problem(**overrides: Any) -> PlanningProblem:
    values: dict[str, Any] = {
        "horizon": Interval(at(7, 0), at(13, 23, 59)),
        "timezone": TZ,
        "availability": windows(7, 13),
        "busy": [],
        "training": TrainingPreferences(free_days=False, time_of_day=None, daylight=False),
    }
    values.update(overrides)
    return PlanningProblem(**values)


def run(p: PlanningProblem, seconds: float = 10) -> PlanResult:
    result = solve(p, time_limit_seconds=seconds)
    assert result.status is not PlanStatus.NO_SOLUTION
    assert validate_plan(p, result) == []
    return result


def minutes(result: PlanResult, task: str) -> int:
    return sum(int(b.duration.total_seconds() // 60) for b in result.blocks if b.task_id == task)


def training(activity_id: str = "t", **kw: Any) -> FlexibleActivity:
    values: dict[str, Any] = {
        "activity_id": activity_id,
        "title": "Entraînement",
        "duration": timedelta(minutes=60),
        "earliest_start": at(8, 0),
        "latest_end": at(8, 23, 59),
        "training": True,
    }
    values.update(kw)
    return FlexibleActivity(**values)


# --- study blocks --------------------------------------------------------------------------------


def test_study_splits_across_disjoint_windows_without_crossing_gap() -> None:
    availability = [Interval(at(7, 9), at(7, 10)), Interval(at(7, 11), at(7, 12))]
    p = problem(availability=availability, demands=[StudyDemand("a", "maths", 90, at(7, 18))])
    result = run(p)
    assert len(result.blocks) == 2
    assert minutes(result, "a") == 90


def test_blocks_are_90_minutes_with_one_shorter_remainder() -> None:
    """C21: 90-minute sessions preferred, 30-90 minutes when necessary."""
    result = run(problem(demands=[StudyDemand("a", "maths", 210, at(13, 23, 59))]))
    lengths = sorted(int(b.duration.total_seconds() // 60) for b in result.blocks)
    assert lengths == [30, 90, 90]


def test_late_locked_effort_is_reserved_but_does_not_meet_a_strict_deadline() -> None:
    locked = PlannedBlock("a", "maths", at(7, 11), at(7, 12), locked=True)
    result = run(
        problem(
            fixed_blocks=[locked],
            demands=[
                StudyDemand("a", "maths", 60, at(7, 10), TargetKind.HARD, completed_minutes=15)
            ],
        )
    )
    assert result.blocks == [locked]
    assert result.status is PlanStatus.PARTIAL
    assert result.hard_deadline_misses == ["a"]
    effort = result.study_effort["a"]
    assert (
        effort.reserved_minutes,
        effort.before_deadline_minutes,
        effort.after_deadline_minutes,
    ) == (60, 0, 60)
    assert (effort.remaining_minutes, effort.unplanned_minutes, effort.completed_minutes) == (
        60,
        0,
        15,
    )


def test_blocks_stay_in_availability_and_outside_busy_time() -> None:
    busy = [Interval(at(7, 9), at(7, 18))]
    p = problem(
        availability=windows(7, 7), busy=busy, demands=[StudyDemand("a", "a", 240, at(7, 23))]
    )
    result = run(p)
    assert minutes(result, "a") == 180  # 08:00-09:00 and 18:00-20:00 only
    assert all(not b.interval.overlaps(busy[0]) for b in result.blocks)
    assert result.unplanned_minutes == {"a": 60}
    assert result.deadline_misses == ["a"]


@pytest.mark.parametrize("kind", [TargetKind.SOFT, TargetKind.HARD])
def test_no_work_after_a_deadline_soft_or_strict(kind: TargetKind) -> None:
    """C30: the feasible part is planned, never anything after the deadline."""
    p = problem(demands=[StudyDemand("a", "a", 30 * 60, at(8, 23, 59), kind)])
    result = run(p)
    assert all(utc(b.end) <= utc(at(8, 23, 59)) for b in result.blocks)
    assert result.unplanned_minutes["a"] > 0
    assert result.deadline_misses == ["a"]
    assert result.hard_deadline_misses == (["a"] if kind is TargetKind.HARD else [])


def test_no_daily_study_cap_beyond_the_availability() -> None:
    """C20: no six-hour limit; the configured availability is the only bound."""
    p = problem(availability=windows(7, 7, 8, 20), demands=[StudyDemand("a", "a", 540, at(7, 23))])
    assert minutes(run(p), "a") == 540


def test_no_per_subject_daily_count() -> None:
    """C23: one subject may get many sessions a day when it needs them."""
    p = problem(availability=windows(7, 7), demands=[StudyDemand("a", "maths", 450, at(7, 23))])
    result = run(p)
    assert len(result.blocks) == 5 and result.unplanned_minutes == {}


def test_break_between_sessions_when_room() -> None:
    """C27: a 15-minute break between study sessions is preferred."""
    p = problem(
        availability=windows(7, 7, 8, 15),
        demands=[
            StudyDemand("a", "maths", 180, at(7, 23)),
            StudyDemand("b", "chimie", 180, at(7, 23)),
        ],
    )
    blocks = sorted(run(p).blocks, key=lambda b: utc(b.start))
    gaps = [utc(n.start) - utc(b.end) for b, n in itertools.pairwise(blocks)]
    assert len(blocks) == 4 and all(g >= timedelta(minutes=15) for g in gaps)


def test_consecutive_sessions_when_necessary() -> None:
    """C27: the break gives way when the time is needed."""
    p = problem(availability=windows(7, 7, 8, 11), demands=[StudyDemand("a", "a", 180, at(7, 23))])
    assert minutes(run(p), "a") == 180


def test_subjects_alternate_when_alternatives_exist() -> None:
    """C22: the same subject is not repeated back to back."""
    p = problem(
        availability=windows(7, 7, 8, 15),
        demands=[
            StudyDemand("a", "maths", 180, at(7, 23)),
            StudyDemand("b", "chimie", 180, at(7, 23)),
        ],
    )
    blocks = sorted(run(p).blocks, key=lambda b: utc(b.start))
    subjects = [b.subject for b in blocks]
    assert all(x != y for x, y in itertools.pairwise(subjects)), subjects


def test_no_study_of_a_subject_right_after_its_class_when_alternatives_exist() -> None:
    """C22: after the chemistry class, another subject first."""
    p = problem(
        availability=[Interval(at(7, 10), at(7, 13, 15))],
        busy=[Interval(at(7, 8), at(7, 10))],
        classes=[ClassSlot(at(7, 8), at(7, 10), "Chimie générale")],
        demands=[
            StudyDemand("c", "Chimie", 90, at(7, 23)),
            StudyDemand("m", "Maths", 90, at(7, 23)),
        ],
    )
    first = min(run(p).blocks, key=lambda b: utc(b.start))
    assert first.subject == "Maths"
    assert same_subject("Chimie", "CHIMIE générale (CM)") and not same_subject("Maths", "Chimie")


def test_work_is_distributed_regularly_until_the_deadline() -> None:
    """C13: regular distribution rather than everything as early as possible."""
    p = problem(demands=[StudyDemand("a", "a", 360, at(10, 23, 59))])
    per_day: dict[date, int] = {}
    for b in run(p).blocks:
        day = b.start.astimezone(TZ).date()
        per_day[day] = per_day.get(day, 0) + int(b.duration.total_seconds() // 60)
    assert len(per_day) == 4 and max(per_day.values()) == 90


def test_task_without_deadline_uses_remaining_time_only() -> None:
    """C24: no artificial deadline; real deadlines are never compromised."""
    p = problem(
        availability=windows(7, 7, 8, 11),
        demands=[
            StudyDemand("free", "lecture", 600, None, priority=3),
            StudyDemand("due", "maths", 90, at(7, 23), priority=1),
        ],
    )
    result = run(p)
    assert minutes(result, "due") == 90 and minutes(result, "free") == 90
    assert result.deadline_misses == []


def test_priority_matters_only_when_time_runs_out() -> None:
    """C9: everything fits -> both full; not enough -> the higher priority wins."""
    roomy = problem(
        demands=[
            StudyDemand("a", "a", 180, at(9, 23), priority=1),
            StudyDemand("b", "b", 180, at(9, 23), priority=3),
        ]
    )
    result = run(roomy)
    assert result.unplanned_minutes == {}
    tight = replace(roomy, availability=windows(7, 7, 8, 11))
    result = run(tight)
    assert minutes(result, "b") == 180 and result.unplanned_minutes == {"a": 180}


def test_fixed_blocks_are_kept_and_count_toward_the_work() -> None:
    kept = PlannedBlock("a", "a", at(7, 9), at(7, 10, 30), locked=True)
    result = run(problem(demands=[StudyDemand("a", "a", 180, at(13, 23))], fixed_blocks=[kept]))
    assert kept in result.blocks and minutes(result, "a") == 180


def test_dst_change_keeps_the_local_quarter_hour_grid() -> None:
    start = datetime(2027, 3, 27, 0, tzinfo=TZ)
    p = PlanningProblem(
        horizon=Interval(start, start + timedelta(days=2)),
        timezone=TZ,
        availability=[
            Interval(datetime(2027, 3, d, 8, tzinfo=TZ), datetime(2027, 3, d, 12, tzinfo=TZ))
            for d in (27, 28)
        ],
        busy=[],
        demands=[StudyDemand("a", "a", 360, datetime(2027, 3, 28, 23, tzinfo=TZ))],
    )
    for block in run(p).blocks:
        local = block.start.astimezone(TZ)
        assert local.minute % 15 == 0 and 8 <= local.hour < 12


# --- stability (C10) ------------------------------------------------------------------------------


def test_identical_replan_keeps_every_block() -> None:
    p = problem(
        demands=[StudyDemand("a", "a", 360, at(11, 23)), StudyDemand("b", "b", 270, at(12, 23))]
    )
    first = run(p)
    second = run(replace(p, previous_blocks=list(first.blocks)))
    assert {(b.task_id, b.start) for b in second.blocks} == {
        (b.task_id, b.start) for b in first.blocks
    }


def test_a_new_commitment_moves_only_the_affected_block() -> None:
    p = problem(demands=[StudyDemand("a", "a", 360, at(11, 23))])
    first = run(p)
    hit = first.blocks[0]
    second = run(replace(p, previous_blocks=list(first.blocks), busy=[hit.interval]))
    kept = {(b.task_id, b.start) for b in first.blocks} & {
        (b.task_id, b.start) for b in second.blocks
    }
    assert len(kept) == len(first.blocks) - 1


def test_moving_is_cheaper_further_in_time() -> None:
    weights = [stability_weight(days) for days in range(0, 14)]
    assert weights == sorted(weights, reverse=True) and weights[0] == STABILITY_COST
    assert weights[1] > weights[7] > weights[13] > 0


# --- flexible activities ---


def test_activity_uses_its_preferred_time_with_travel_reserved() -> None:
    item = training(
        preferred_start=at(8, 18),
        travel_before=timedelta(minutes=40),
        travel_after=timedelta(minutes=30),
        training=False,
    )
    result = run(problem(activities=[item]))
    (placed,) = result.activities
    assert placed.start == at(8, 18)
    assert placed.reserved == Interval(at(8, 17, 20), at(8, 19, 30))


def test_activity_without_room_is_unplaced_not_forced() -> None:
    item = training(earliest_start=at(8, 10), latest_end=at(8, 11), training=False)
    result = run(problem(activities=[item], busy=[Interval(at(8, 10), at(8, 10, 30))]))
    assert result.unplaced == ["t"] and result.status is PlanStatus.PARTIAL


def test_allowed_intervals_are_hard() -> None:
    allowed = (Interval(at(8, 7), at(8, 9)),)
    result = run(problem(activities=[training(allowed=allowed)]))
    assert allowed[0].contains(Interval(result.activities[0].start, result.activities[0].end))


def test_coach_order_and_explicit_recovery() -> None:
    """C28: the coach's order and only explicit recovery days."""
    first = training(
        "a", earliest_start=at(7, 6), latest_end=at(13, 22), sequence_group="w", sequence_index=0
    )
    second = training(
        "b",
        earliest_start=at(7, 6),
        latest_end=at(13, 22),
        sequence_group="w",
        sequence_index=1,
        rest_days=2,
    )
    result = run(problem(activities=[second, first]))
    placed = {p.activity_id: p for p in result.activities}
    gap = (placed["b"].start.date() - placed["a"].start.date()).days
    assert utc(placed["a"].end) <= utc(placed["b"].start) and gap >= 2


def test_coach_order_survives_absent_middle_session() -> None:
    items = [
        training("a", earliest_start=at(8, 11), latest_end=at(8, 12), sequence_index=0),
        training("b", allowed=(), sequence_index=1),
        training("c", earliest_start=at(8, 9), latest_end=at(8, 10), sequence_index=2),
    ]
    result = run(problem(activities=[replace(item, sequence_group="week") for item in items]))
    assert "b" in result.unplaced
    assert len(result.activities) == 1


def test_recovery_after_a_fixed_race() -> None:
    item = training(earliest_start=at(8, 6), latest_end=at(10, 22), rest_days=2)
    result = run(problem(activities=[item], sport_days=[date(2026, 12, 7)]))
    assert result.activities[0].start.date() >= date(2026, 12, 9)


def test_training_prefers_days_without_classes_then_mornings() -> None:
    """C11: free days first, then mornings, then afternoons, evenings last."""
    week = training(earliest_start=at(7, 6), latest_end=at(9, 22))
    prefs = TrainingPreferences(free_days=True, time_of_day="morning", daylight=False)
    classes = [ClassSlot(at(d, 10), at(d, 12), "Cours") for d in (7, 9)]
    result = run(problem(activities=[week], training=prefs, classes=classes))
    start = result.activities[0].start.astimezone(TZ)
    assert start.date() == date(2026, 12, 8) and start.hour < 12


def test_evening_preference_is_supported() -> None:
    """A user who prefers evenings gets the evening (same mechanism, other period)."""
    week = training(earliest_start=at(8, 6), latest_end=at(8, 22))
    prefs = TrainingPreferences(free_days=False, time_of_day="evening", daylight=False)
    start = run(problem(activities=[week], training=prefs)).activities[0].start.astimezone(TZ)
    assert start.hour >= 17


def test_time_of_day_order_morning_afternoon_evening() -> None:
    assert (
        0
        < TIME_OF_DAY_STEP_COST
        < TIME_OF_DAY_FAR_COST
        < CLASS_DAY_COST
        < BETWEEN_CLASSES_COST
        < DARK_COST
    )


def test_training_avoids_the_dark_but_is_not_forbidden_there() -> None:
    sun = lambda _day: (at(8, 8, 30), at(8, 16, 40))  # noqa: E731 - test double
    prefs = TrainingPreferences(free_days=False, time_of_day=None, daylight=True)
    day = run(problem(activities=[training()], training=prefs, sun=sun))
    start = day.activities[0].start
    assert at(8, 8, 30) <= start and utc(day.activities[0].end) <= utc(at(8, 16, 40))
    night = training(earliest_start=at(8, 18), latest_end=at(8, 21))
    dark = run(problem(activities=[night], training=prefs, sun=sun))
    assert dark.unplaced == []
    indoor = run(problem(activities=[replace(night, outdoor=False)], training=prefs, sun=sun))
    assert indoor.unplaced == []


def test_training_between_two_classes_is_avoided() -> None:
    """C1: not between two classes, even when there would be time."""
    classes = [ClassSlot(at(8, 8), at(8, 10), "A"), ClassSlot(at(8, 16), at(8, 18), "B")]
    result = run(
        problem(
            activities=[training()],
            classes=classes,
            busy=[Interval(c.start, c.end) for c in classes],
        )
    )
    start = result.activities[0].start
    assert utc(start) >= utc(at(8, 18)) or utc(start) < utc(at(8, 8))


def test_temporary_preference_holds_then_expires() -> None:
    """C29: a temporary preference overrides the permanent one only in its period."""
    late = TemporaryPreference(at(8, 0), at(9, 0), activity_hours=(time(18), time(22)))
    p = problem(activities=[training()], temporary=[late])
    assert run(p).activities[0].start.astimezone(TZ).hour >= 18
    later = replace(training(), earliest_start=at(9, 0), latest_end=at(9, 23, 59))
    assert p.hours_on(date(2026, 12, 9)) == (time(6), time(22))
    assert run(replace(p, activities=[later])).outside_hours == []


def test_categories_compete_only_through_priority_and_deadline() -> None:
    """C25: one free slot, a training and study with the same deadline: priority decides."""
    slot = [Interval(at(8, 9), at(8, 10, 30))]
    study = StudyDemand("s", "maths", 90, at(8, 10, 30), priority=3)
    sport = training(
        earliest_start=at(8, 9),
        latest_end=at(8, 10, 30),
        duration=timedelta(minutes=90),
        priority=1,
    )
    base = problem(
        horizon=Interval(at(8, 0), at(8, 23)),
        availability=slot,
        demands=[study],
        activities=[sport],
    )
    result = run(base)
    assert minutes(result, "s") == 90 and result.unplaced == ["t"]
    swapped = replace(
        base, demands=[replace(study, priority=1)], activities=[replace(sport, priority=3)]
    )
    result = run(swapped)
    assert result.activities and result.unplanned_minutes == {"s": 90}


# --- objective balance ---


def test_preferences_never_outweigh_placing_work() -> None:
    """Every soft cost one item can accumulate stays below leaving it unplanned (C9, C11, C24)."""
    general = CLASS_DAY_COST + TIME_OF_DAY_FAR_COST
    assert general < PREFERRED_START_COST < DARK_COST  # an explicit time beats general prefs
    activity_worst = (
        PREFERRED_START_COST
        + OUTSIDE_HOURS_COST
        + DARK_COST
        + BETWEEN_CLASSES_COST
        + CLASS_DAY_COST
        + TIME_OF_DAY_FAR_COST
        + COACH_DAY_COST * 6
        + 2 * STABILITY_COST
    )
    assert activity_worst < UNPLANNED_PER_MINUTE * 30  # shortest activity, priority 1
    block_worst = SHORT_BLOCK_COST + MISSING_BREAK_COST + 2 * SAME_SUBJECT_COST + 2 * STABILITY_COST
    block_worst += DISTRIBUTION_PER_MINUTE * 90
    assert block_worst < NO_DEADLINE_PER_MINUTE * 30
    assert NO_DEADLINE_PER_MINUTE * 3 < UNPLANNED_PER_MINUTE


def test_lower_bound() -> None:
    assert objective_lower_bound({"a": 180}, 90, 30) == 0
    assert objective_lower_bound({"a": 210}, 90, 30) == SHORT_BLOCK_COST
    assert objective_lower_bound({"a": 165}, 90, 30) == 30


def test_two_week_realistic_plan_is_complete_in_time() -> None:
    subjects = [f"s{i}" for i in range(6)]
    classes = [
        ClassSlot(at(d, 10), at(d, 12), "Analyse")
        for d in range(7, 21)
        if date(2026, 12, d).weekday() < 5
    ]
    p = PlanningProblem(
        horizon=Interval(at(7, 0), at(20, 23, 59)),
        timezone=TZ,
        availability=windows(7, 20),
        busy=[Interval(c.start, c.end) for c in classes],
        demands=[StudyDemand(s, s, 600, at(20, 23, 59)) for s in subjects],
        activities=[
            training(f"t{d}", earliest_start=at(d, 6), latest_end=at(d + 2, 22))
            for d in (7, 10, 14, 17)
        ],
        classes=classes,
        training=TrainingPreferences(),
    )
    began = clock.monotonic()
    result = run(p, seconds=10)
    assert clock.monotonic() - began < 15
    assert result.status is PlanStatus.COMPLETE, (result.unplanned_minutes, result.unplaced)
