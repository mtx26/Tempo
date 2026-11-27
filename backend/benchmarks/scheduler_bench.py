"""Reproducible CP-SAT benchmark (milestone 12). Run from backend/:

    uv run python benchmarks/scheduler_bench.py            # summary table
    uv run python benchmarks/scheduler_bench.py --json     # machine-readable

Each case is solved with the production settings (8 workers) and a fixed time limit; the
script reports the solver status, objective, wall time and whether the independent validator
accepts the plan. Deterministic inputs (no randomness).
"""

import json
import sys
import time as clock
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tempo.domain.models import TargetKind
from tempo.scheduler.model import FlexibleActivity, PlanningProblem, StudyDemand
from tempo.scheduler.solver import solve
from tempo.scheduler.validate import validate_plan
from tempo.timeutil import Interval

TZ = ZoneInfo("Europe/Brussels")
START = date(2027, 2, 1)  # a Monday


@dataclass(frozen=True)
class Case:
    name: str
    weeks: int
    subjects: int
    hours_each: int
    classes_per_day: int


CASES = [
    Case("2w-4s", 2, 4, 6, 2),
    Case("4w-6s", 4, 6, 10, 2),
    Case("8w-8s", 8, 8, 14, 2),
]


def problem(case: Case) -> PlanningProblem:
    days = case.weeks * 7
    availability, busy = [], []
    for offset in range(days):
        day = START + timedelta(days=offset)
        availability.append(
            Interval(
                datetime.combine(day, time(8), tzinfo=TZ),
                datetime.combine(day, time(21), tzinfo=TZ),
            )
        )
        if day.weekday() < 5:
            for index in range(case.classes_per_day):
                begin = datetime.combine(day, time(9 + 4 * index), tzinfo=TZ)
                busy.append(Interval(begin, begin + timedelta(hours=2)))
    demands = []
    for index in range(case.subjects):
        target_day = START + timedelta(days=days - 1 - (index % 3) * 5)
        demands.append(
            StudyDemand(
                task_id=f"t{index}",
                subject=f"subject{index}",
                remaining_minutes=case.hours_each * 60,
                target=datetime.combine(target_day + timedelta(days=1), time(0), tzinfo=TZ),
                target_kind=TargetKind.HARD if index % 4 == 3 else TargetKind.SOFT,
            )
        )
    return PlanningProblem(
        horizon=Interval(
            datetime.combine(START, time(0), tzinfo=TZ),
            datetime.combine(START + timedelta(days=days), time(0), tzinfo=TZ),
        ),
        timezone=TZ,
        availability=availability,
        busy=busy,
        demands=demands,
    )


def run(limit: float) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for case in CASES:
        p = problem(case)
        began = clock.perf_counter()
        result = solve(p, time_limit_seconds=limit)
        elapsed = clock.perf_counter() - began
        rows.append(
            {
                "case": case.name,
                "status": result.status.value,
                "solver": result.solver_status,
                "seconds": round(elapsed, 2),
                "blocks": len(result.blocks),
                "unplanned": sum(result.unplanned_minutes.values()),
                "deadline_misses": len(result.deadline_misses),
                "objective": result.objective,
                "valid": validate_plan(p, result) == [],
            }
        )
    return rows


def compare_hints(limit: float) -> list[dict[str, object]]:
    base = problem(CASES[0])
    prior = solve(base, time_limit_seconds=max(5, limit), workers=1)
    if not prior.blocks:
        raise RuntimeError("benchmark needs a previously published plan")
    first = prior.blocks[0]
    unavailable = [*base.busy, first.interval]
    disjoint = [
        part
        for span in base.availability
        for part in (
            Interval(span.start, span.start + timedelta(hours=3)),
            Interval(span.end - timedelta(hours=4), span.end),
        )
    ]
    flexible = FlexibleActivity(
        activity_id="training",
        title="Training",
        duration=timedelta(minutes=60),
        earliest_start=base.availability[1].start,
        latest_end=base.availability[1].end,
        previous_start=base.availability[1].start + timedelta(hours=3),
    )
    previous = replace(base, previous_blocks=prior.blocks)
    cases = {
        "ancien planning": previous,
        "nouvelle indisponibilite": replace(previous, busy=unavailable),
        "fenetres disjointes": replace(previous, busy=unavailable, availability=disjoint),
        "activite flexible": replace(previous, busy=unavailable, activities=[flexible]),
    }
    rows = []
    for name, case in cases.items():
        for hints in (True, False):
            for _ in range(3):
                began = clock.perf_counter()
                result = solve(case, time_limit_seconds=limit, warm_start=hints)
                rows.append(
                    {
                        "case": name,
                        "hints": hints,
                        "seconds": round(clock.perf_counter() - began, 3),
                        "solver": result.solver_status,
                        "objective": result.objective,
                        "valid": validate_plan(case, result) == [],
                        "unplanned": sum(result.unplanned_minutes.values()),
                        "kept": len(
                            {(b.task_id, b.start, b.end) for b in prior.blocks}
                            & {(b.task_id, b.start, b.end) for b in result.blocks}
                        ),
                    }
                )
    return rows


if __name__ == "__main__":
    limit = float(next((a for a in sys.argv[1:] if a.replace(".", "").isdigit()), "10"))
    results = compare_hints(limit) if "--compare-hints" in sys.argv else run(limit)
    if "--json" in sys.argv:
        print(json.dumps(results, indent=2))
    else:
        for row in results:
            print(" | ".join(f"{k}={v}" for k, v in row.items()))
