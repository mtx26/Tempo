# Study planner (milestone 3)

Code: `backend/src/tempo/scheduler/` — `model.py` (types), `solver.py` (OR-Tools CP-SAT),
`validate.py` (independent deterministic validator). The solver never decides alone: every result
must pass `validate_plan` before it can be proposed (production wiring: milestone 7).

## Inputs (`PlanningProblem`)
- `horizon`, `timezone` (local days and DST handled through UTC arithmetic).
- `availability`: windows where study is allowed (configured in Home Assistant).
- `busy`: fixed commitments — iCal classes, races, and later travel blocks (milestone 4).
- `demands`: remaining effort per task with `target` instant and `target_kind` (SOFT default).
- `locked_blocks`: accepted blocks the user locked; never moved, counted toward effort.
- `preferences`: 90-min preferred block, 30–90 min allowed, 15-min grid, 30-min gap between
  same-subject blocks, daily cap (360 min), max 2 blocks per subject per day.

## Hard constraints
No overlap with busy intervals, locked blocks or outside availability; block length bounds;
blocks of a family stay within one local day; daily cap; HARD target ⇒ no block ends after it.

## Soft objective (weights in `solver.py`)
1. Unplanned effort (weight > horizon length, so planning late always beats not planning).
2. Lateness of soft-target blocks (minutes past target).
3. Back-to-back same-subject blocks (alternation).
4. Blocks not of the preferred length.
5. Overshoot (rounding a remainder up to the minimum block length).

## Outputs (`PlanResult`)
`status` COMPLETE / PARTIAL (see `unplanned_minutes`) / NO_SOLUTION (solver produced nothing:
only locked blocks are returned, nothing is invented); `late_minutes` for soft targets;
`hard_deadline_misses`; `conflicts` (e.g. a class moved onto a locked block → approval needed);
`solver_status` (OPTIMAL / FEASIBLE …).

## Verified behaviour (tests/test_scheduler.py, tests/test_validate.py)
90-min splitting, remainder block, busy/availability, short windows, alternation, daily cap,
soft target overrun reported, impossible hard deadline reported (never violated), locked blocks,
locked block vs moved class conflict, empty availability, DST day, 4-week/6-subject simulation.

## Hardening after adversarial review (agent `scheduler`)
Fixed with regression tests: start domains restricted to free grid slots (6-week / 6-subject /
90 h case: COMPLETE in ~3.5 s, previously NO_SOLUTION); soft target already in the past no longer
blocks planning; busy times with seconds are widened (MOTIS returns seconds); alternation and the
per-day maximum apply per *subject* across tasks; daily cap includes locked blocks; duplicate
task ids rejected; grid aligned to local midnight; contiguous availability windows merged.
Validator now checks every overlapping pair, grid alignment, per-subject daily maximum and that a
COMPLETE status really covers all effort. Unplanned-effort weight dominates all soft penalties
even when targets lie beyond the horizon (a regression caught by tests during the fix).

## Known limitations
- 4-week, 6-subject case with two daily classes still uses the full 10 s and returns FEASIBLE
  (complete and valid, not proven optimal). A greedy warm start is a possible improvement.
- An availability window crossing local midnight is split into two days; same-subject blocks on
  both sides of midnight are not penalised.
- No stability term yet: re-optimisation may move unlocked blocks freely (milestone 7 will add a
  penalty for moving previously proposed blocks to avoid churn).
- Travel time between locations is not modelled yet (milestone 4 adds travel as busy intervals
  and location-aware availability).

## Milestone 12 — performance (2026-11-23, dev PC, 8 workers, 10 s limit, median of 3 runs)
`uv run python benchmarks/scheduler_bench.py` (deterministic cases: weekday classes, 08:00-21:00
availability, soft + hard targets).

| Case | Before | + proven lower bound | + greedy warm start (current) |
|---|---|---|---|
| 2 weeks, 4 subjects | 0.12 s OPTIMAL | 0.14 s | 0.11 s OPTIMAL |
| 4 weeks, 6 subjects | 10.07 s FEASIBLE (not proven) | 1.56 s proven | **0.39 s proven** |
| 8 weeks, 8 subjects | 10.16 s FEASIBLE (not proven) | 4.47 s proven | **1.00 s proven** |
| 8 weeks, 8 subjects, mornings only | 10.26 s FEASIBLE | — | 10.33 s, same objective |

- **Lower bound** (`objective_lower_bound`): effort not divisible by the preferred length costs at
  least one short block or a small overshoot; all other penalties are >= 0, so a plan reaching
  the bound is optimal (`solver_status = OPTIMAL_BY_BOUND`) and the search stops.
- **Warm start** (`_Builder.warm_start`): greedy day-by-day placement by urgency given as CP-SAT
  hints; never constraining (tests check the objective is never worse than without hints).
- **Midnight**: same-subject blocks on both sides of midnight are penalised like same-day
  adjacent blocks (only pairs that can come within the minimum gap of midnight).

## Requirement fixes (2026-11-24, D026)
- Long-term targets beyond the horizon: proportional share only (no false impossibility).
- `max_blocks_per_subject_per_day` is soft: second pass for tasks with unplanned effort only.
- Missed sessions: active-plan blocks only; a session under way is never moved.
