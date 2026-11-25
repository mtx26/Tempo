"""Milestone 12: proven lower bound, greedy warm start, same-subject adjacency across midnight."""

from datetime import timedelta

import pytest

from benchmarks import scheduler_bench
from tempo.scheduler.model import Preferences
from tempo.scheduler.solver import objective_lower_bound, solve
from tempo.scheduler.validate import validate_plan
from tempo.timeutil import Interval
from tests.test_scheduler import at, demand, problem


@pytest.mark.parametrize(
    ("need", "bound"),
    [(540, 0), (600, 30), (630, 0), (570, 30), (90, 0), (60, 30)],
)
def test_lower_bound(need: int, bound: int) -> None:
    assert objective_lower_bound({"t": need}, Preferences()) == bound


def test_warm_start_never_worsens_and_is_proven_optimal_on_benchmark_cases() -> None:
    for case in scheduler_bench.CASES[:2]:
        p = scheduler_bench.problem(case)
        warm = solve(p, time_limit_seconds=10, warm_start=True)
        cold = solve(p, time_limit_seconds=10, warm_start=False)
        assert validate_plan(p, warm) == []
        assert warm.objective is not None and cold.objective is not None
        assert warm.objective <= cold.objective
        assert warm.solver_status in ("OPTIMAL", "OPTIMAL_BY_BOUND")


def late_evening_and_early_morning(subjects: list[str]) -> list[Interval]:
    return [Interval(at(2, 22, 30), at(3, 0)), Interval(at(3, 0), at(3, 1, 30))]


def test_same_subject_is_not_placed_on_both_sides_of_midnight_when_avoidable() -> None:
    p = problem(
        [demand("maths", 90), demand("physique", 90)],
        late_evening_and_early_morning([]),
        days=(1, 4),
    )
    result = solve(p, time_limit_seconds=10)
    assert validate_plan(p, result) == []
    evening = next(b for b in result.blocks if b.start == at(2, 22, 30))
    morning = next(b for b in result.blocks if b.start == at(3, 0))
    assert evening.subject != morning.subject


def test_single_subject_still_uses_both_windows_when_needed() -> None:
    p = problem([demand("maths", 180)], late_evening_and_early_morning([]), days=(1, 4))
    result = solve(p, time_limit_seconds=10)
    assert validate_plan(p, result) == []
    assert sum((b.end - b.start for b in result.blocks), timedelta(0)) == timedelta(minutes=180)


def test_per_subject_daily_count_is_soft_when_feasibility_needs_it() -> None:
    from tempo.domain.models import TargetKind

    p = problem(
        [demand("examen", 360, target_day=1, kind=TargetKind.HARD)],
        [Interval(at(1, 8), at(1, 20))],
        days=(1, 2),
    )
    result = solve(p, time_limit_seconds=10)
    assert validate_plan(p, result) == []
    assert result.unplanned_minutes == {} and result.hard_deadline_misses == []
    assert len([b for b in result.blocks if b.start.day == 1]) == 4  # beyond the preferred 2


def test_preferred_count_is_kept_when_there_is_room() -> None:
    p = problem([demand("maths", 360)], [Interval(at(d, 8), at(d, 20)) for d in range(1, 8)])
    result = solve(p, time_limit_seconds=10)
    per_day: dict[int, int] = {}
    for block in result.blocks:
        per_day[block.start.day] = per_day.get(block.start.day, 0) + 1
    assert max(per_day.values()) <= 2
