"""Behavioural tests for the CP-SAT study planner.

Assertions target properties required by docs/REQUIREMENTS.md, not exact placements, because
CP-SAT may return any optimal solution. Every produced plan is also checked by the independent
deterministic validator.
"""

import itertools
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tempo.domain.models import TargetKind
from tempo.scheduler.model import (
    Interval,
    PlannedBlock,
    PlanningProblem,
    PlanResult,
    PlanStatus,
    Preferences,
    StudyDemand,
)
from tempo.scheduler.solver import solve
from tempo.scheduler.validate import validate_plan

TZ = ZoneInfo("Europe/Brussels")


def at(day: int, hour: int, minute: int = 0, month: int = 12, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def window(day: int, start_hour: int, end_hour: int) -> Interval:
    return Interval(at(day, start_hour), at(day, end_hour))


def end_of(day: int) -> datetime:
    return datetime.combine(date(2026, 12, day) + timedelta(days=1), time(0), tzinfo=TZ)


def demand(
    task_id: str,
    minutes: int,
    target_day: int = 20,
    kind: TargetKind = TargetKind.SOFT,
    subject: str | None = None,
) -> StudyDemand:
    return StudyDemand(
        task_id=task_id,
        subject=subject or task_id,
        remaining_minutes=minutes,
        target=end_of(target_day),
        target_kind=kind,
    )


def problem(
    demands: list[StudyDemand],
    availability: list[Interval],
    busy: list[Interval] | None = None,
    locked: list[PlannedBlock] | None = None,
    days: tuple[int, int] = (1, 7),
    preferences: Preferences | None = None,
) -> PlanningProblem:
    return PlanningProblem(
        horizon=Interval(at(days[0], 0), end_of(days[1])),
        timezone=TZ,
        availability=availability,
        busy=busy or [],
        demands=demands,
        locked_blocks=locked or [],
        preferences=preferences or Preferences(),
    )


def solve_valid(p: PlanningProblem) -> PlanResult:
    result = solve(p, time_limit_seconds=10)
    assert validate_plan(p, result) == []
    return result


def minutes(blocks: list[PlannedBlock], task_id: str) -> int:
    return sum(int(b.duration.total_seconds() // 60) for b in blocks if b.task_id == task_id)


def test_task_is_split_into_preferred_90_minute_blocks() -> None:
    result = solve_valid(problem([demand("analyse", 180)], [window(1, 8, 18)]))
    assert result.status is PlanStatus.COMPLETE
    assert sorted(b.duration for b in result.blocks) == [timedelta(minutes=90)] * 2


def test_remainder_uses_one_shorter_block() -> None:
    result = solve_valid(problem([demand("analyse", 120)], [window(1, 8, 18)]))
    durations = sorted(int(b.duration.total_seconds() // 60) for b in result.blocks)
    assert durations == [30, 90]


def test_blocks_avoid_fixed_events_and_stay_in_availability() -> None:
    p = problem(
        [demand("analyse", 240)],
        [window(1, 8, 12), window(2, 8, 12)],
        busy=[Interval(at(1, 9), at(1, 11)), Interval(at(2, 8), at(2, 10))],
    )
    result = solve_valid(p)
    assert minutes(result.blocks, "analyse") == 240  # exactly the free capacity
    assert result.status is PlanStatus.COMPLETE


def test_effort_beyond_free_capacity_is_reported_as_unplanned() -> None:
    p = problem(
        [demand("analyse", 270)],
        [window(1, 8, 12), window(2, 8, 12)],
        busy=[Interval(at(1, 9), at(1, 11)), Interval(at(2, 8), at(2, 10))],
    )
    result = solve_valid(p)
    assert result.status is PlanStatus.PARTIAL
    assert result.unplanned_minutes == {"analyse": 30}


def test_short_window_allows_shorter_block_rather_than_nothing() -> None:
    result = solve_valid(problem([demand("analyse", 60)], [window(1, 8, 9)]))
    assert minutes(result.blocks, "analyse") == 60


def test_same_subject_blocks_are_not_back_to_back_when_avoidable() -> None:
    p = problem(
        [demand("analyse", 180), demand("chimie", 180)],
        [window(1, 8, 14)],
    )
    result = solve_valid(p)
    assert result.status is PlanStatus.COMPLETE
    ordered = sorted(result.blocks, key=lambda b: b.start)
    for first, second in itertools.pairwise(ordered):
        if first.task_id == second.task_id:
            assert second.start - first.end >= timedelta(minutes=30)


def test_daily_study_cap_is_respected() -> None:
    prefs = Preferences(max_daily_study_minutes=180)
    p = problem([demand("analyse", 360)], [window(1, 8, 20), window(2, 8, 20)], preferences=prefs)
    result = solve_valid(p)
    assert result.status is PlanStatus.COMPLETE
    assert {b.start.date() for b in result.blocks} == {date(2026, 12, 1), date(2026, 12, 2)}


def test_soft_target_is_exceeded_and_reported_instead_of_failing() -> None:
    p = problem(
        [demand("analyse", 180, target_day=1)],
        [window(1, 8, 10), window(3, 8, 12)],
    )
    result = solve_valid(p)
    assert result.status is PlanStatus.COMPLETE
    assert minutes(result.blocks, "analyse") == 180
    assert result.late_minutes["analyse"] > 0
    assert result.hard_deadline_misses == []


def test_impossible_hard_deadline_is_reported_never_violated() -> None:
    p = problem(
        [demand("analyse", 300, target_day=1, kind=TargetKind.HARD)],
        [window(1, 8, 11), window(3, 8, 18)],
    )
    result = solve_valid(p)
    assert all(b.end <= end_of(1) for b in result.blocks)
    assert result.status is PlanStatus.PARTIAL
    assert result.unplanned_minutes["analyse"] == 120
    assert result.hard_deadline_misses == ["analyse"]


def test_locked_block_is_kept_and_counts_toward_effort() -> None:
    locked = PlannedBlock("analyse", "analyse", at(1, 8), at(1, 9, 30), locked=True)
    p = problem([demand("analyse", 180)], [window(1, 8, 18)], locked=[locked])
    result = solve_valid(p)
    assert locked in result.blocks
    assert minutes(result.blocks, "analyse") == 180


def test_no_availability_returns_explicit_unplanned_effort() -> None:
    result = solve_valid(problem([demand("analyse", 90)], []))
    assert result.blocks == []
    assert result.status is PlanStatus.PARTIAL
    assert result.unplanned_minutes == {"analyse": 90}


def test_planning_across_dst_change_keeps_local_wall_clock() -> None:
    day = datetime(2027, 3, 28, 0, 0, tzinfo=TZ)
    p = PlanningProblem(
        horizon=Interval(day, day + timedelta(days=1)),
        timezone=TZ,
        availability=[Interval(day.replace(hour=8), day.replace(hour=12))],
        busy=[],
        demands=[
            StudyDemand(
                "analyse",
                "analyse",
                90,
                target=day + timedelta(days=1),
                target_kind=TargetKind.SOFT,
            )
        ],
        locked_blocks=[],
        preferences=Preferences(),
    )
    result = solve_valid(p)
    (block,) = result.blocks
    assert 8 <= block.start.astimezone(TZ).hour < 12


def test_class_moved_onto_locked_block_is_reported_not_infeasible() -> None:
    locked = PlannedBlock("analyse", "analyse", at(1, 8), at(1, 9, 30), locked=True)
    p = problem(
        [demand("analyse", 180)],
        [window(1, 8, 18)],
        busy=[Interval(at(1, 9), at(1, 10))],
        locked=[locked],
    )
    result = solve(p, time_limit_seconds=10)
    assert result.status is PlanStatus.COMPLETE
    assert locked in result.blocks
    assert len(result.conflicts) == 1
    new_blocks = [b for b in result.blocks if not b.locked]
    assert all(not b.interval.overlaps(Interval(at(1, 9), at(1, 10))) for b in new_blocks)


def test_zero_remaining_effort_plans_nothing() -> None:
    result = solve_valid(problem([demand("analyse", 0)], [window(1, 8, 18)]))
    assert result.blocks == []
    assert result.status is PlanStatus.COMPLETE


def test_realistic_four_week_simulation_is_complete_and_valid() -> None:
    subjects = ["analyse", "algebre", "physique", "chimie", "info", "anglais"]
    demands = [demand(s, 600, target_day=26) for s in subjects]  # 60 h in total
    availability, busy = [], []
    for day in range(1, 29):
        availability.append(window(day, 8, 12))
        availability.append(window(day, 13, 19))
        if date(2026, 12, day).weekday() < 5:  # weekday classes
            busy.append(Interval(at(day, 9), at(day, 11)))
            busy.append(Interval(at(day, 14), at(day, 16)))
    p = problem(demands, availability, busy=busy, days=(1, 28))
    result = solve_valid(p)
    assert result.status is PlanStatus.COMPLETE
    assert result.late_minutes == {}


def test_inconsistent_preferences_are_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        Preferences(min_block_minutes=120, preferred_block_minutes=90)
    with pytest.raises(ValueError):
        Preferences(preferred_block_minutes=100)  # not a multiple of 15


# --- Regression tests from the adversarial review (milestone 3) ---------------------------


def test_soft_target_long_past_still_plans_everything() -> None:
    p = problem([demand("analyse", 180, target_day=1)], [window(15, 8, 18)], days=(11, 16))
    result = solve_valid(p)
    assert result.status is PlanStatus.COMPLETE
    assert result.late_minutes["analyse"] == 180


def test_busy_interval_with_seconds_is_never_overlapped() -> None:
    busy = [Interval(at(1, 8), at(1, 8).replace(second=30))]
    result = solve_valid(problem([demand("analyse", 90)], [window(1, 8, 10)], busy=busy))
    assert result.status is PlanStatus.COMPLETE


def test_alternation_applies_per_subject_across_tasks() -> None:
    p = problem(
        [
            demand("maths-1", 180, subject="maths"),
            demand("maths-2", 180, subject="maths"),
            demand("chimie", 180),
        ],
        [window(1, 8, 20), window(2, 8, 20)],
    )
    result = solve_valid(p)
    assert result.status is PlanStatus.COMPLETE
    ordered = sorted(result.blocks, key=lambda b: b.start)
    for first, second in itertools.pairwise(ordered):
        if first.subject == second.subject and first.start.date() == second.start.date():
            assert second.start - first.end >= timedelta(minutes=30)


def test_daily_cap_counts_locked_blocks() -> None:
    locked = PlannedBlock("analyse", "analyse", at(1, 8), at(1, 11), locked=True)
    prefs = Preferences(max_daily_study_minutes=180, max_block_minutes=180)
    p = problem(
        [demand("analyse", 360)],
        [window(1, 8, 20), window(2, 8, 20)],
        locked=[locked],
        preferences=prefs,
    )
    result = solve_valid(p)
    assert all(b.start.date() != date(2026, 12, 1) for b in result.blocks if not b.locked)


def test_duplicate_task_ids_are_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        problem([demand("analyse", 180), demand("analyse", 90)], [window(1, 8, 18)])


def test_blocks_are_aligned_to_local_quarter_hours_when_horizon_is_not() -> None:
    p = PlanningProblem(
        horizon=Interval(at(1, 0, 7).replace(second=13), end_of(2)),
        timezone=TZ,
        availability=[window(1, 8, 18)],
        busy=[],
        demands=[demand("analyse", 90)],
        locked_blocks=[],
    )
    (block,) = solve_valid(p).blocks
    assert block.start.minute % 15 == 0 and block.start.second == 0


def test_contiguous_availability_windows_are_usable_as_one() -> None:
    windows = [Interval(at(1, 8), at(1, 9)), Interval(at(1, 9), at(1, 9, 30))]
    result = solve_valid(problem([demand("analyse", 90)], windows))
    assert result.status is PlanStatus.COMPLETE


def test_six_week_six_subject_plan_is_complete_within_time_limit() -> None:
    subjects = ["analyse", "algebre", "physique", "chimie", "info", "anglais"]
    demands = [demand(s, 900, target_day=31) for s in subjects]  # 90 h in total
    availability, busy = [], []
    start = date(2026, 12, 1)
    for offset in range(42):
        d = start + timedelta(days=offset)
        for begin, end in ((8, 12), (13, 19), (20, 22)):
            availability.append(
                Interval(
                    datetime.combine(d, time(begin), tzinfo=TZ),
                    datetime.combine(d, time(end), tzinfo=TZ),
                )
            )
        if d.weekday() < 5:
            busy.append(
                Interval(
                    datetime.combine(d, time(9), tzinfo=TZ),
                    datetime.combine(d, time(11), tzinfo=TZ),
                )
            )
    p = PlanningProblem(
        horizon=Interval(
            datetime.combine(start, time(0), tzinfo=TZ), end_of(1) + timedelta(days=41)
        ),
        timezone=TZ,
        availability=availability,
        busy=busy,
        demands=demands,
        locked_blocks=[],
    )
    result = solve_valid(p)
    assert result.status is PlanStatus.COMPLETE
