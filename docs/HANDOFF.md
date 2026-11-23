# HANDOFF — state of Tempo and everything NOT yet done

Written 2026-11-23 for a future model/session. **This file is authoritative over the older
"Status" lines of `PROGRESS.md`, some of which are stale.** Reply to the user in French (CLAUDE.md).

---

## 1. What exists and is verified (local commits, nothing pushed or deployed)

| Commit | Content | Verified how |
|---|---|---|
| `43a9091` | Phase 0 feasibility (`docs/FEASIBILITY.md`), backend skeleton (FastAPI, typed `Settings`, bearer token), domain models, iCal import + diff | unit tests |
| `3e51017` | M3 CP-SAT study planner + independent validator (`docs/SCHEDULER.md`) | unit tests, 6-week simulation |
| `220cda8` | M2b PostgreSQL schema, Alembic 0001, safe idempotent iCal sync with audit | integration tests on a REAL local PostgreSQL 16 |
| `8e86135` | M4 transport: places, phone position freshness, origin inference, MOTIS client, mandatory travel, travel-aware study windows (`docs/TRANSPORT.md`) | unit tests + fakes |
| `e84bafd` | M4b real MOTIS v2.11.4 in Docker (Brussels region), 7 live integration tests (`routing/README.md`) | live tests against the real local MOTIS |
| (M4c) | Nationwide benchmark, 10 Belgian feeds, GTFS-RT relay, TEC repair, alerts | 255 tests + live portal/national MOTIS |
| `d89ba6e` | M6a private API for HA: `/v1/tasks` (list/create/patch), `/v1/location`, Alembic 0002 `phone_position` | integration tests on real PostgreSQL |

Last full run (at `d89ba6e`): **198 tests passed**, coverage 98 %, `ruff check`, `ruff format --check`,
`mypy --strict src tests` clean, pip-audit clean (at M4). Separate repo `../tempo-homeassistant`:
commit `c485e2c` = scaffold only (hacs.json, manifest with `OWNER` placeholders, empty `__init__.py`).

### How to run the checks (Windows, Git Bash)
```bash
# PostgreSQL for integration tests (keep running in another terminal / background):
cd backend && uv run --no-project --python 3.12 --with pgserver python scripts/dev_postgres.py .pgdata
#  -> prints TEMPO_TEST_DATABASE_URL=postgresql+psycopg://postgres:@127.0.0.1:<port>/postgres  (port changes each start)
# MOTIS (Docker Desktop must be running; docker.exe is NOT on PATH by default):
export PATH="$LOCALAPPDATA/Programs/DockerDesktop/resources/bin:$PATH"
docker start tempo-motis-brussels        # container + volume tempo-routing-brussels already exist
cd backend
TEMPO_TEST_DATABASE_URL=... TEMPO_TEST_MOTIS_URL=http://127.0.0.1:18080 uv run pytest -q --cov=tempo
uv run ruff format --check . && uv run ruff check . && uv run mypy src tests
```
Without the two env vars, integration tests are SKIPPED with an explicit reason (never silently passed).
The MOTIS Brussels data expires: STIB GTFS of 2026-11-23 is valid until **2026-12-20**; MOTIS loaded
30 days from import. After that, re-run `python routing/fetch_data.py brussels` + the `import` command
from `routing/README.md`, otherwise `test_motis_live.py` fails (correct signal, not a code bug).

---

## 2. Interrupted work (finish or redo first)

1. ~~Nationwide MOTIS import benchmark~~ — **DONE in M4c** (the import had in fact succeeded:
   3 min 29 s, exit 0). Results in `routing/README.md`. Text below kept for history.
   **Nationwide MOTIS import benchmark — (was) interrupted.**
   Data downloaded to `routing/data/belgium/` (957 MB: OSM 700 MB, De Lijn 184 MB, TEC 97 MB, STIB, SNCB;
   manifest.json present). The import container `tempo-motis-import-be` (volume `tempo-routing-belgium`,
   `--memory 7g`, config `routing/config/belgium.yml`) was started in background; the session ended before
   any result. To do: `docker rm -f tempo-motis-import-be` if it still exists, re-run the import command
   (same as Brussels with `belgium` paths), record import time, peak RAM, data size, query latency in
   `routing/README.md` + `docs/FEASIBILITY.md` §3, run a few nationwide queries (e.g. Liège → Gent).
   Disk C: had ~30 GB free — check before starting.
2. **HA dev environment in `../tempo-homeassistant` — not set up.** The user REJECTED the command that
   created `pyproject.toml` + ran `uv sync` with `pytest-homeassistant-custom-component`; yet untracked
   `pyproject.toml` and `uv.lock` exist there. Ask the user before installing Home Assistant test deps
   (large download) and before keeping/deleting those two files.

---

## 3. Remaining roadmap (not started unless stated)

### M1 leftovers (backend foundation)
- Docker image for the backend: DONE (`backend/Dockerfile`, API + relay; worker entry point later).
- `docker-compose`/dev stack (api + postgres + motis) — not written.
- CI `.github/workflows/backend.yml` exists (with Postgres service + pip-audit) but has **never run**
  (no remote). It does not start MOTIS, so live MOTIS tests will be skipped in CI.
- Server entrypoint `python -m tempo.api` written (`server_header=False`). Still open: 401 rate
  limiting, two valid tokens for rotation (security review items).

### M2 leftovers (domain and sync)
- **Availability rules** (weekly windows configured in HA, tagged with a place) — no model/table/API yet.
- iCal **fetch** (HTTP download of the university feed URL, kept as a secret, never in DB) — not written;
  only parsing + persistence exist. Must parse window + margin (tests use 60 days), see D017.
- Pair "series master moved" as changed instead of removed+added (D012 known limitation).
- Flag floating times in DST gap/overlap (low priority).
- Location aliases / places configuration storage (currently only in-memory `PlaceDirectory`).

### M3 leftovers (scheduler)
- **Stability term**: re-optimisation may move unlocked blocks freely → penalise moving previously
  proposed blocks (needed before automatic nightly re-planning).
- Greedy warm start (4-week/6-subject case still uses the full 10 s, FEASIBLE not OPTIMAL).
- Same-subject adjacency across midnight not penalised.
- Wiring: nothing yet builds a `PlanningProblem` from the database (tasks + fixed events + travel +
  availability) or persists `schedule_version` / `study_block` rows.

### M4 leftovers (transport)
- M4c done: 10 feeds + relay + TEC repair + alerts (docs/TRANSPORT.md "Belgian feeds", D021/D022).
  Remaining: Standard key from the user (secret `tempo_relay_bmc_partner_key`) and verification that
  the portal host accepts it; relay long-running test; gzip support unknown (~6 GB/day downloads at
  default intervals — consider longer intervals on the home server); SNCB 10 % unresolved trip
  updates not investigated; relay has no Dockerfile/Swarm service yet.
- M4d done as a library (`departure.py`); scheduling of checks + HA notifications = M7/M6.
- Relay deployment: one replica, persistent `TEMPO_RELAY_STATE_FILE`; `belgium.yml` with relay
  URLs was not re-imported as-is (keys match the MOTIS-dumped config; verify on first import).
- STIB has no GTFS-RT: optional later adapter using STIB waiting-times JSON API.
- Delay handling only verified on synthetic data (no delay observed live).
- No MOTIS pagination (`nextPageCursor`); one malformed leg invalidates the whole answer (strict).
- Real-time re-checks (planning time, evening before, near departure), disruption alerts and multiple
  departure reminders → M7.

### M5 training sync (NOT STARTED)
- Narrow, explicitly authorized email ingestion (specific sender/thread only, never whole mailbox) —
  needs the user's authorization and credentials strategy first.
- Training = flexible (day/time movable, prescribed content/duration kept); races = fixed.
- Scheduler support for flexible training sessions (generic "flexible activity" with fixed duration).

### M6 Home Assistant integration (only M6a backend API done)
- Backend API still missing: schedule/calendar read (`GET /v1/schedule?from&to`), proposals
  list/approve/reject, availability windows, places/aliases, sensors data (next departure, remaining effort).
- In `../tempo-homeassistant` (separate public-ready repo, D010): config flow (URL + token, validated via
  `/v1/status`; choose the `device_tracker`), coordinator, calendar, todo, sensors, services
  (e.g. `tempo.add_study_task`), position push on tracker state change → `POST /v1/location`,
  notifications and approval actions, conversation agent where supported, translations (fr), brand
  `icon.png`, licence, hassfest + `hacs/action` CI. Replace `OWNER` placeholders. **Never publish
  without explicit approval.**

### M7 continuous operations (NOT STARTED)
- Worker: scheduled iCal sync, re-planning, route re-checks, notifications, recovery.
- Failed sync must be recorded in a separate transaction (security review item).
- Approval workflow for protected changes (iCal change overnight → update source, propose, wait).
- LLM layer (interpretation only; backend must work without it) — provider not chosen.

### M8/M9 production readiness and deployment (NOT STARTED; requires explicit approval to deploy)
- Swarm stack (api, worker, postgres ≥ 15, motis), internal networks only, Docker secrets, two DB roles
  (migration role vs runtime DML role, append-only `audit_entry`), one-shot migration job, backups +
  restore drill, health checks, rollback, resource benchmark on the real server.

---

## 4. Decisions/actions needed from the user
- Belgian Mobility Standard account (free) → API key as a secret.
- LLM provider and secret management strategy.
- Phone `device_tracker` entity id, university iCal URL, places (home, campus, …) and aliases, study
  availability windows.
- Authorization scope for training emails (sender/thread) — M5.
- Licence + GitHub owner for `tempo-homeassistant`; approval before any publication/push.
- Whether to install the HA test toolkit (§2.2).

## 5. Traps already hit (avoid repeating)
- **Bash tool + heredocs**: several commands failed with `unexpected EOF while looking for matching '`
  when heredocs contained Python triple quotes or byte literals; nothing ran. Use the Write/Edit tools
  for source/test files.
- `\r\n` inside Python strings written through a heredoc became real line breaks (broken literal).
- Python compares/subtracts aware datetimes **with the same tzinfo as wall-clock time** (wrong across
  DST). Always convert to UTC first (`tempo.scheduler.model.utc`). Two real bugs came from this.
- `recurring-ical-events` adds RECURRENCE-ID to single events, swaps inverted DTSTART/DTEND and
  reinterprets unknown TZIDs → guarded by a raw pre-scan in `tempo/sync/ics.py`.
- `pgserver` has no wheels for Python 3.13 → run it with `--python 3.12`.
- `docker` CLI not on PATH; `docker-credential-desktop` must be on PATH for builds; use
  `MSYS_NO_PATHCONV=1` and Windows paths (`cygpath -w`) for volume mounts in Git Bash.
- Starlette 1.10 TestClient needs `httpx2` (not `httpx`); `httpx2` is also the runtime HTTP client.
- Alembic `env.py` takes an advisory lock; never run Alembic concurrently in threads of one process
  (its context is process-global) — test with subprocesses.
- Auto-mode classifier blocked commits until the user explicitly allowed them; the user later
  authorized local commits (CLAUDE.md "Autonomous development policy").

## 6. Repository items NOT created by the development sessions (do not delete/commit blindly)
Untracked in this repo: `docs/audits/claude-environment-audit.md`, `graphify-out/`, `.mcp.json`,
`.claude/settings.json.graphify-bak`, `temp.txt`; modified: `.claude/settings.json`,
`CLAUDE.md` (graphify section). These come from the user or other tools — ask before committing them.
