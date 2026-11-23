# Progress

## Status (2026-11-23)
- Phase 0 (feasibility): desk research done → `docs/FEASIBILITY.md`. MOTIS Belgium benchmark NOT done (needs server/Docker).
- Milestone 1 (backend foundation): PARTIAL — `backend/` uv project, typed `Settings`, FastAPI app factory,
  public `/health`, bearer-protected `/v1/status`. Postgres, Alembic, Docker dev stack: not started.
  CI workflow `.github/workflows/backend.yml` written but **never executed** (not a git repo, no remote).
- Milestone 2 (domain and sync): STARTED — `FixedEvent`, `StudyTask` (soft target by default), ICS expansion
  (`tempo.sync.ics.parse_ics`) and sync diff (`diff_events`). No persistence yet.
- Milestone 3 (scheduler): DONE — CP-SAT planner `tempo.scheduler.solver.solve` + independent validator
  `tempo.scheduler.validate.validate_plan` (docs/SCHEDULER.md). Reviewed adversarially by agent `scheduler`;
  10 findings fixed with regression tests.
- Milestone 2b (persistence): IN PROGRESS, uncommitted — SQLAlchemy models, Alembic scaffolding, integration
  tests against a local pgserver PostgreSQL 16 (D016). Initial migration and ICS repository not written yet.
- Deployment: none. Local commits only: `43a9091` (phase 0 + M1 + ICS), M3 commit (see git log).

## Quality gates (last local run, backend/)
- M3 validation: `uv run pytest -q --ignore=tests/integration`: 89 passed, coverage 98 % (scheduler, domain,
  sync, api, config); ruff + mypy strict pass on those packages; pip-audit: no known vulnerabilities.
- `uv run ruff check .` / `ruff format --check .`: pass.
- `uv run mypy src tests` (strict): pass.
- PostgreSQL / MOTIS integration tests: none yet (no Docker locally).

## Next actions (exact)
1. Persistence: write initial Alembic revision in `backend/src/tempo/db/alembic/versions/`, implement
   `tempo/db/ics_repository.py` (`get_or_create_source`, `apply_ics_sync`, `active_events`), make
   `tests/integration/test_persistence.py` pass with `TEMPO_TEST_DATABASE_URL` from `scripts/dev_postgres.py`.
2. Split repositories (temps.txt §4): create local public-ready repo for the HA integration (sibling dir).
3. Travel constraints (M4) with a MOTIS client contract + mocks (no claim of real validation).

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
