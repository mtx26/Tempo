"""Re-planning stability: previously proposed blocks stay put unless moving buys something."""

from dataclasses import replace

from tempo.scheduler.model import PlannedBlock
from tempo.scheduler.solver import solve
from tempo.scheduler.validate import validate_plan
from tests.test_scheduler import at, demand, problem, window

AVAILABILITY = [window(d, 8, 20) for d in range(1, 8)]


def starts(blocks: list[PlannedBlock]) -> set[tuple[str, str]]:
    return {(b.task_id, b.start.isoformat()) for b in blocks}


def test_identical_replan_keeps_every_block() -> None:
    base = problem([demand("maths", 270), demand("physics", 180)], AVAILABILITY)
    first = solve(base, time_limit_seconds=10)
    again = replace(base, previous_blocks=first.blocks)
    second = solve(again, time_limit_seconds=10)
    assert validate_plan(again, second) == []
    assert starts(second.blocks) == starts(first.blocks)


def test_hand_placed_previous_blocks_are_preferred_over_equivalent_ones() -> None:
    previous = [
        PlannedBlock("maths", "maths", at(3, 14), at(3, 15, 30)),
        PlannedBlock("maths", "maths", at(5, 9), at(5, 10, 30)),
    ]
    p = problem([demand("maths", 180)], AVAILABILITY)
    result = solve(replace(p, previous_blocks=previous), time_limit_seconds=10)
    assert starts(result.blocks) == starts(previous)


def test_new_commitment_moves_only_the_affected_block() -> None:
    previous = [
        PlannedBlock("maths", "maths", at(3, 14), at(3, 15, 30)),
        PlannedBlock("maths", "maths", at(5, 9), at(5, 10, 30)),
    ]
    p = problem([demand("maths", 180)], AVAILABILITY, busy=[window(3, 13, 17)])
    p = replace(p, previous_blocks=previous)
    result = solve(p, time_limit_seconds=10)
    assert validate_plan(p, result) == []
    assert ("maths", at(5, 9).isoformat()) in starts(result.blocks)
    assert ("maths", at(3, 14).isoformat()) not in starts(result.blocks)
    assert sum(int(b.duration.total_seconds() // 60) for b in result.blocks) == 180


def test_finished_task_blocks_are_simply_dropped() -> None:
    previous = [PlannedBlock("gone", "gone", at(3, 14), at(3, 15, 30))]
    p = problem([demand("maths", 90)], AVAILABILITY)
    result = solve(replace(p, previous_blocks=previous), time_limit_seconds=10)
    assert {b.task_id for b in result.blocks} == {"maths"}
