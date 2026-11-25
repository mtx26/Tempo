"""Study preferences are soft: never stronger than feasibility (ARCHITECTURE.md §2)."""

from tempo.domain.models import TargetKind
from tempo.scheduler.model import Preferences
from tempo.scheduler.solver import solve
from tempo.scheduler.validate import validate_plan
from tests.test_scheduler import demand, problem, window


def test_daily_cap_is_exceeded_rather_than_leaving_work_unplanned() -> None:
    # Only Monday is free and the work is due that day: the 2 h cap must give way.
    p = problem(
        [demand("maths", 300, target_day=1, kind=TargetKind.HARD)],
        [window(1, 8, 20)],
        preferences=Preferences(max_daily_study_minutes=120),
    )
    result = solve(p, time_limit_seconds=10)
    assert validate_plan(p, result) == []
    assert result.unplanned_minutes == {}


def test_daily_cap_is_respected_when_other_days_allow_it() -> None:
    p = problem(
        [demand("maths", 240, target_day=5)],
        [window(d, 8, 20) for d in range(1, 6)],
        preferences=Preferences(max_daily_study_minutes=120),
    )
    result = solve(p, time_limit_seconds=10)
    per_day: dict[int, int] = {}
    for block in result.blocks:
        day = block.start.day
        per_day[day] = per_day.get(day, 0) + int(block.duration.total_seconds() // 60)
    assert max(per_day.values()) <= 120


def test_blocks_per_subject_give_way_to_a_strict_deadline() -> None:
    # One free day, 6 h of one subject: far more blocks than the preferred 2 per day.
    p = problem(
        [demand("chimie", 360, target_day=1, kind=TargetKind.HARD)],
        [window(1, 8, 20)],
        preferences=Preferences(max_daily_study_minutes=1440),
    )
    result = solve(p, time_limit_seconds=10)
    assert validate_plan(p, result) == []
    assert result.unplanned_minutes == {}
    assert len(result.blocks) > 2
