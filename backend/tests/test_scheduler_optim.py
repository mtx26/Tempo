"""Milestone 12: proven lower bound, greedy warm start, same-subject adjacency across midnight."""

from datetime import timedelta

import pytest

from benchmarks import scheduler_bench
from tempo.scheduler.model import Interval, Preferences
from tempo.scheduler.solver import objective_lower_bound, solve
from tempo.scheduler.validate import validate_plan
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
