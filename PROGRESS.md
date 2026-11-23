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
- Milestone 2b (persistence): DONE — SQLAlchemy 2 models, Alembic migration 0001 (reversible, concurrency-safe),
  safe idempotent ICS sync repository with audit (D017, D018). Reviewed by agent `security`; findings fixed with
  tests (false deletions, suspect feeds, concurrency, audit detail, overlong UIDs). Validated against a REAL local
  PostgreSQL 16 (pgserver, D016); production PostgreSQL on Swarm not tested.
- Milestone 4 (transport): CODE DONE — places/aliases, phone position freshness, origin inference, MOTIS
  `/api/v6/plan` client (httpx2), mandatory travel blocks, travel-aware study windows, end-to-end with the
  planner. Reviewed by agent `transport` (9 findings fixed). **MOTIS NOT validated against a real server.**
- Repository split (D010): `../tempo-homeassistant` created locally (commit `c485e2c`, scaffold only, OWNER
  placeholders, not published). `homeassistant/` removed from this repo.
- Deployment: none. Local commits only: `43a9091` (phase 0 + M1 + ICS), `3e51017` (M3), `220cda8` (M2b), M4 commit (see git log).

## Quality gates (last local run, backend/)
- Last full run (M4): 170 passed (15 integration on real PostgreSQL 16), coverage 98 %; ruff, mypy strict, pip-audit clean.
- Previous run (M2b): `TEMPO_TEST_DATABASE_URL=… uv run pytest -q --cov=tempo`: 106 passed (15 integration on
  real PostgreSQL 16), coverage 98 %; ruff + `mypy --strict src tests` pass; pip-audit: no known vulnerabilities.
- Without `TEMPO_TEST_DATABASE_URL`, the 15 integration tests are reported SKIPPED (explicit reason).
- `uv run ruff check .` / `ruff format --check .`: pass.
- `uv run mypy src tests` (strict): pass.
- PostgreSQL / MOTIS integration tests: none yet (no Docker locally).

## Next actions (exact)
1. Local MOTIS smoke test with Docker Desktop (available since 2026-11-23): MOTIS v2.11.x linux binary,
   STIB + SNCB GTFS (anonymous BMC downloads, CC BY 4.0), small OSM extract; check the client against real
   responses; record findings in docs/TRANSPORT.md. Disk: 31 GB free — keep data under ~2 GB.
2. M6 HA integration in `../tempo-homeassistant`; M5 training sync; M7 worker/approvals; M9 deployment docs.

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
