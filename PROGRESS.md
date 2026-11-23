# Progress

## Status (2026-11-23)
- Phase 0 (feasibility): desk research done → `docs/FEASIBILITY.md`. MOTIS Belgium benchmark NOT done (needs server/Docker).
- Milestone 1 (backend foundation): PARTIAL — `backend/` uv project, typed `Settings`, FastAPI app factory,
  public `/health`, bearer-protected `/v1/status`. Postgres, Alembic, Docker dev stack: not started.
  CI workflow `.github/workflows/backend.yml` written but **never executed** (not a git repo, no remote).
- Milestone 2 (domain and sync): STARTED — `FixedEvent`, `StudyTask` (soft target by default), ICS expansion
  (`tempo.sync.ics.parse_ics`) and sync diff (`diff_events`). No persistence yet.
- Deployment: none. No commits (directory is not a git repository).

## Quality gates (last local run, backend/)
- `uv run pytest -q --cov=tempo`: 49 passed, coverage 97 %.
- `uv run ruff check .` / `ruff format --check .`: pass.
- `uv run mypy src tests` (strict): pass.
- PostgreSQL / MOTIS integration tests: none yet (no Docker locally).

## Next milestone
1. Milestone 3 core in pure Python: OR-Tools CP-SAT study-block placement (fixed events, availability,
   90-min preference, subject alternation, soft vs hard targets, infeasibility report), with simulations.
2. Persistence (SQLAlchemy + Alembic) once a PostgreSQL instance is available for integration tests.

## Session log
- Initial starter files and requirements authored.
- 2026-11-23: environment audit, feasibility research (MOTIS v2.11.4, Belgian Mobility portal & terms,
  HACS/HA manifest rules), backend skeleton, domain models, ICS import. TDD found two real library issues
  (non-recurring events got RECURRENCE-ID → unstable keys; inverted DTSTART/DTEND silently swapped) — fixed with
  regression tests (D012).
- 2026-11-23: independent reviews (agents `tester`, `security`). Fixed with regression tests: crash on mixed
  floating/UTC times, invalid override dropping a whole series, duplicate UIDs silently lost, unknown TZID silently
  reinterpreted, false removals when the sync window moves, duplicate skipped entries; token echoed in validation
  errors, public `/openapi.json`, trivial tokens accepted, no Docker-secret support. Open low-priority items:
  series master move pairing (D012), DST-gap floating times, `uv audit`/pip-audit in CI, uvicorn `server_header=False`
  and 401 rate limiting when the server entrypoint is written, token rotation with two valid tokens.

## Blockers / decisions requiring user
- D010: HACS repository layout (dedicated public integration repo vs. public monorepo).
- Create a free Belgian Mobility developer account (Standard tier) before live polling; key stored as a secret.
- Install Docker Desktop (or use WSL Ubuntu) for local PostgreSQL/MOTIS tests; C: has only 32 GB free.
- MOTIS Belgium resource benchmark must run on the Swarm node — requires approval and access.
- Choose LLM API provider and secret management strategy.
- Confirm phone `device_tracker` entity ID and iCal source URL when the integration is implemented.
- Context7 plugin needs authentication to be usable (optional).
- `git init` + first commit only with your approval.
