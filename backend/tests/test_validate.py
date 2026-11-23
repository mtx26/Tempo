"""The validator is the deterministic safety net: it must catch plans that break hard rules."""

from datetime import datetime
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
from tempo.scheduler.validate import validate_plan

TZ = ZoneInfo("Europe/Brussels")


def at(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 12, day, hour, minute, tzinfo=TZ)


def make_problem(kind: TargetKind = TargetKind.SOFT) -> PlanningProblem:
    return PlanningProblem(
        horizon=Interval(at(0), at(0, day=3)),
        timezone=TZ,
        availability=[Interval(at(8), at(18))],
        busy=[Interval(at(12), at(13))],
        demands=[StudyDemand("a", "a", 180, target=at(16), target_kind=kind)],
        locked_blocks=[],
        preferences=Preferences(),
    )


def plan(*blocks: PlannedBlock) -> PlanResult:
    return PlanResult(status=PlanStatus.COMPLETE, blocks=list(blocks))


def block(start: datetime, end: datetime) -> PlannedBlock:
    return PlannedBlock("a", "a", start, end)


def test_valid_plan_has_no_violations() -> None:
    blocks = [block(at(8), at(9, 30)), block(at(10), at(11, 30))]
    assert validate_plan(make_problem(), plan(*blocks)) == []


def test_overlap_with_busy_interval_is_detected() -> None:
    violations = validate_plan(make_problem(), plan(block(at(11), at(12, 30))))
    assert any("busy" in v for v in violations)


def test_block_outside_availability_is_detected() -> None:
    violations = validate_plan(make_problem(), plan(block(at(18), at(19, 30))))
    assert any("availability" in v for v in violations)


def test_overlapping_blocks_are_detected() -> None:
    violations = validate_plan(make_problem(), plan(block(at(8), at(9, 30)), block(at(9), at(10))))
    assert any("overlap" in v for v in violations)


def test_block_longer_than_maximum_is_detected() -> None:
    violations = validate_plan(make_problem(), plan(block(at(8), at(10))))
    assert any("duration" in v for v in violations)


def test_hard_deadline_violation_is_detected() -> None:
    violations = validate_plan(make_problem(TargetKind.HARD), plan(block(at(16), at(17, 30))))
    assert any("deadline" in v for v in violations)


def test_overplanning_beyond_rounding_is_detected() -> None:
    blocks = [block(at(8), at(9, 30)), block(at(10), at(11, 30)), block(at(14), at(15, 30))]
    violations = validate_plan(make_problem(), plan(*blocks))
    assert any("more than requested" in v for v in violations)


def test_dropping_a_locked_block_is_detected() -> None:
    problem = make_problem()
    locked = PlannedBlock("a", "a", at(8), at(9, 30), locked=True)
    problem = PlanningProblem(
        horizon=problem.horizon,
        timezone=TZ,
        availability=problem.availability,
        busy=problem.busy,
        demands=problem.demands,
        locked_blocks=[locked],
        preferences=problem.preferences,
    )
    violations = validate_plan(problem, plan(block(at(10), at(11, 30))))
    assert any("locked" in v for v in violations)


def test_block_for_unknown_task_is_detected() -> None:
    stray = PlannedBlock("ghost", "ghost", at(8), at(9, 30))
    assert any("unknown task" in v for v in validate_plan(make_problem(), plan(stray)))


def test_block_outside_horizon_is_detected() -> None:
    violations = validate_plan(make_problem(), plan(block(at(8, day=4), at(9, 30, day=4))))
    assert any("horizon" in v for v in violations)


def test_daily_cap_violation_is_detected() -> None:
    problem = make_problem()
    problem = PlanningProblem(
        horizon=problem.horizon,
        timezone=TZ,
        availability=problem.availability,
        busy=[],
        demands=[StudyDemand("a", "a", 600, target=at(0, day=3))],
        locked_blocks=[],
        preferences=Preferences(max_daily_study_minutes=120),
    )
    blocks = [block(at(8), at(9, 30)), block(at(10), at(11, 30))]
    assert any("daily cap" in v for v in validate_plan(problem, plan(*blocks)))


def test_non_adjacent_overlap_is_detected() -> None:
    blocks = [
        PlannedBlock("a", "a", at(8), at(10), locked=True),
        PlannedBlock("a", "a", at(8, 30), at(9), locked=True),
        block(at(9, 15), at(10)),
    ]
    violations = validate_plan(make_problem(), plan(*blocks))
    assert any("overlap" in v for v in violations)


def test_complete_status_without_enough_blocks_is_detected() -> None:
    violations = validate_plan(make_problem(), plan(block(at(8), at(9, 30))))
    assert any("complete" in v for v in violations)


def test_off_grid_block_is_detected() -> None:
    violations = validate_plan(make_problem(), plan(block(at(8, 1), at(8, 46))))
    assert any("grid" in v for v in violations)


def test_too_many_blocks_of_one_subject_per_day_is_detected() -> None:
    blocks = [block(at(8), at(8, 30)), block(at(9), at(9, 30)), block(at(10), at(10, 30))]
    violations = validate_plan(make_problem(), plan(*blocks))
    assert any("blocks of" in v for v in violations)
