# Progress

> **Read `docs/HANDOFF.md` first**: authoritative state as of 2026-11-23 (commit `d89ba6e`), interrupted work,
> full list of what is NOT done, pending user decisions and known traps. Some status lines below are stale.

## Status (2026-11-23)
- **M4c (Belgian realtime feeds): DONE locally** — nationwide MOTIS benchmark finished (import 3 min 29 s,
  1.8 GB, server 0.7–0.9 GB RAM, median query 100–130 ms); 10 Belgian feeds verified by real requests
  and registered (`tempo.transport.feeds`); GTFS-RT relay (`tempo.transport.rt_relay`: key secret,
  quota guard, back-off, freshness 503, TEC repair 50 % → 100 %); service alerts parsed and filtered
  by impact period (D021, D022); real +7 min live delay observed. Standard key not yet available.
- **M1 image: DONE locally** — `backend/Dockerfile` (pinned digests, non-root, read-only FS OK),
  `python -m tempo.api` entry point (`server_header=False`); smoke-tested, see docs/DEPLOYMENT.md.
- **M4d (departure re-check): DONE (library)** — `tempo.transport.departure.check_departure`: fresh
  phone GPS only, live routing, statuses on_time/leave_now/leave_earlier/at_risk/late/impossible/
  unknown_*. Not yet scheduled by a worker nor exposed to HA (M7/M6).
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
  planner. Reviewed by agent `transport` (9 findings fixed). Commit `8e86135`.
- Milestone 4b (real MOTIS): DONE for the Brussels test region — local Docker MOTIS v2.11.4 (pinned SHA-256),
  STIB + SNCB GTFS, SNCB GTFS-RT, BBBike OSM; import 20 s, 182 MB, ~103 MB RAM; 7 live integration tests
  pass; real response recorded as fixture; MOTIS error reasons surfaced. Nationwide config prepared, not run.
- Repository split (D010): `../tempo-homeassistant` created locally (commit `c485e2c`, scaffold only, OWNER
  placeholders, not published). `homeassistant/` removed from this repo.
- Deployment: none. Local commits only: `43a9091` (phase 0 + M1 + ICS), `3e51017` (M3), `220cda8` (M2b), M4 commit (see git log).

## Quality gates (last local run, backend/)
- M4c + M4d: 278 passed with real PostgreSQL 16 + real MOTIS Brussels (10 skipped there, run
  separately: nationwide MOTIS 7/7, live portal 3/3), coverage 98 %; ruff, ruff format, mypy strict
  clean; pip-audit: no known vulnerabilities. Adversarial review by agent `transport`: 3 bugs + 5
  issues fixed with regression tests; its "stale data kept by MOTIS" hypothesis was tested live
  and refuted (MOTIS drops realtime when the relay answers 503).
- Last full run (M4b, PostgreSQL + MOTIS live): 179 passed, coverage 98 %; ruff, mypy strict clean.
- Previous run (M4): 170 passed (15 integration on real PostgreSQL 16), coverage 98 %; ruff, mypy strict, pip-audit clean.
- Previous run (M2b): `TEMPO_TEST_DATABASE_URL=… uv run pytest -q --cov=tempo`: 106 passed (15 integration on
  real PostgreSQL 16), coverage 98 %; ruff + `mypy --strict src tests` pass; pip-audit: no known vulnerabilities.
- Without `TEMPO_TEST_DATABASE_URL`, the 15 integration tests are reported SKIPPED (explicit reason).
- `uv run ruff check .` / `ruff format --check .`: pass.
- `uv run mypy src tests` (strict): pass.
- PostgreSQL / MOTIS integration tests: none yet (no Docker locally).

## Next actions (exact)
1. M1 leftovers: dev compose stack (api + postgres + relay + motis); API 401 rate limiting and
   token rotation (two valid tokens) — security items before any deployment.
2. M6 HA integration in `../tempo-homeassistant` (needs user OK for HA test toolkit); M5 training
   sync; M7 worker/approvals; M9 deployment docs.
Local services: PostgreSQL via `backend/scripts/dev_postgres.py` (if pgserver times out after an
unclean stop, postgres may still come up on the printed port — check `.pgdata/log`); MOTIS
`tempo-motis-brussels` (18080); nationwide volume `tempo-routing-belgium` (server
`tempo-motis-belgium`, stopped) and test copy `tempo-routing-belgium-rttest` (container
`tempo-motis-be-rttest`, stopped, config points to a relay on host.docker.internal:8090).

## Session log
- 2026-11-23 (M4c): nationwide benchmark, 10 feeds verified, relay + TEC repair, alerts policy.
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
- Create a free Belgian Mobility developer account (Standard tier); key → Docker secret
  `tempo_relay_bmc_partner_key`. Then run `TEMPO_TEST_BMC_KEY=... TEMPO_TEST_PORTAL_LIVE=1 pytest
  tests/integration/test_portal_live.py` to verify the key header on this host (unverified).
- Install Docker Desktop (or use WSL Ubuntu) for local PostgreSQL/MOTIS tests; C: has only 32 GB free.
- MOTIS Belgium resource benchmark must run on the Swarm node — requires approval and access.
- Choose LLM API provider and secret management strategy.
- Confirm phone `device_tracker` entity ID and iCal source URL when the integration is implemented.
- Context7 plugin needs authentication to be usable (optional).
- `git init` + first commit only with your approval.
