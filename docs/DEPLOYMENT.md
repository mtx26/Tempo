# Deployment strategy (prepared and validated locally — NOT deployed; requires explicit approval)

Target: existing Debian Docker Swarm managed by Portainer, with Home Assistant already deployed. No public Tempo API. Prefer internal network and service credentials; protect DB and GTFS volumes, limit capabilities, verify container images, avoid mounting Docker socket. Use Docker secrets or an equivalent external secret manager. Never place HA tokens, email credentials, LLM keys or location history in Git.

Before production: verify resources for Belgian graph and MOTIS, test HA connection, test external feed terms, configure backups, health checks, rate limits, rolling upgrade/rollback, disaster recovery. Require explicit deployment approval. `deploy/` is intentionally empty until feasibility is validated.

## Database requirements gathered so far (not yet executable)
- PostgreSQL >= 15 (unique constraint with `NULLS NOT DISTINCT`); dev/test uses 16.
- Two roles: a migration role (DDL) used by a one-shot migration job, and a runtime role with only DML
  rights; `audit_entry` limited to INSERT/SELECT for the runtime role (append-only audit).
- A failed sync must be recorded in a separate transaction by the worker (milestone 7).

## Images (built and smoke-tested locally 2026-11-23; nothing pushed or deployed)
| Image | Source | Notes |
|---|---|---|
| `tempo-backend` | `backend/Dockerfile` | uv 0.13.9 + python:3.13-slim-bookworm pinned by digest, multi-stage, non-root uid 10002, 599 MB (venv 307 MB, mostly OR-Tools). Default `python -m tempo.api` (port 8000); relay `python -m tempo.transport.rt_relay` (port 8090). |
| `tempo-routing` | `routing/Dockerfile` | MOTIS v2.11.4 release binary pinned by SHA-256, non-root. |

Smoke test (local, `--read-only --tmpfs /tmp`): API `/health` 200, no `Server` header, `/v1/status`
401 without token and 200 with the token read from `/run/secrets/tempo_api_token`; token not in
logs. Relay `/health` 200 in anonymous mode without calling the portal.

Service requirements for the future stack:
- `tempo-api`: **replicas: 1** (in-memory auth limiter); secrets `tempo_api_token`,
  `tempo_database_url`, optional `tempo_api_token_previous` during a rotation (D023: deploy new token
  as `tempo_api_token` + old one as previous, update HA, then remove the previous secret).
- `tempo-rt-relay`: **replicas: 1**; secret `tempo_relay_bmc_partner_key` (optional, Standard
  tier); `TEMPO_RELAY_STATE_FILE` on a small persistent volume; outbound HTTPS to
  `api-management-discovery-production.azure-api.net` only; reachable by `tempo-routing` only.
- `tempo-routing`: data volume from `motis import`, config `routing/config/belgium.yml`.
- Health checks: the image has no curl; use
  `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"`.
- Done: 401 limiting and token rotation (D023). Still open before deployment: worker,
  migration job, DB roles, backups/restore drill, resource benchmark on the Swarm node.

## Local development stack (`deploy/compose.dev.yml`, tested 2026-11-23)
`python deploy/make_dev_secrets.py` (git-ignored `deploy/secrets/`), then
`docker compose -f deploy/compose.dev.yml up -d`: postgres 16 → one-shot `python -m tempo.db upgrade`
(reads only the DB URL secret) → api (127.0.0.1:18000) ; rt-relay (internal `routing` network +
egress, persistent request log) ; routing (127.0.0.1:18081).
Verified: migration to head, task created and listed through the API with the token from the
secret file, relay unreachable from the API, PostgreSQL not published, token absent from logs.
Limitations: `front` network gives api/routing outbound Internet (dev only); the routing volume must
be re-imported with `routing/config/belgium.yml` before use, otherwise MOTIS polls the portal
directly; dev Postgres uses one role (the two-role model is still to do).

## Swarm deployment (prepared and validated locally; NOT executed — needs explicit approval)

Files: `deploy/stack.yml`, `deploy/db/init-roles.sh`, `deploy/backup/{backup,restore_drill}.sh`.
Validated: `docker stack config` resolves the stack (and refuses to without `TEMPO_REGISTRY` /
`TEMPO_VERSION`); the role chain was run for real on PostgreSQL 16.16 (init script creates
`tempo_app`, migration + `grant-runtime` as `tempo_owner`, API serving reads/writes as
`tempo_app`, which is neither superuser nor allowed to create roles; audit append-only per
`tests/integration/test_db_roles.py`).

### 1. Prerequisites (on the Swarm manager, by the user)
- Registry reachable by the nodes; push `tempo-backend:<version>` (`backend/Dockerfile`) and
  `tempo-routing:v2.11.4` (`routing/Dockerfile`).
- Overlay network shared with Home Assistant (attachable): e.g.
  `docker network create -d overlay --attachable homeassistant_tempo`, and attach the HA service
  to it. Set `TEMPO_HA_NETWORK` if the name differs.
- Routing volume: run `motis import` with `routing/config/belgium.yml` into the stack's
  `routing-data` volume on the node that will run MOTIS (see routing/README.md); ~1.8 GB, ~3.5 min
  locally; re-run when the GTFS feeds are refreshed (STIB feed validity ~4 weeks).
- Pin `routing` and `postgres` to a node with a placement constraint if the cluster has several
  nodes (local volumes).

### 2. Secrets (never in Git; created once with `docker secret create <name> -`)
| Secret | Content |
|---|---|
| `tempo_owner_db_password` | random password of the schema owner |
| `tempo_app_db_password` | random password of the runtime role |
| `tempo_owner_database_url` | `postgresql+psycopg://tempo_owner:<pw>@postgres:5432/tempo` |
| `tempo_database_url` | `postgresql+psycopg://tempo_app:<pw>@postgres:5432/tempo` |
| `tempo_api_token` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` (also entered in HA) |
| `tempo_ics_url` | university iCal URL |
| optional `tempo_relay_bmc_partner_key`, `tempo_anthropic_api_key`, `tempo_training_imap_password` | see TRANSPORT.md, D021, TRAINING_EMAILS.md |

### 3. Deploy / update / rollback
- Deploy: `TEMPO_REGISTRY=... TEMPO_VERSION=x.y.z docker stack deploy -c deploy/stack.yml tempo`.
  The `migrate` job (replicated-job) upgrades the schema and re-grants `tempo_app`; api and worker
  retry until the schema exists.
- Update: same command with the new version; `api`/`worker` use stop-first + automatic rollback
  on failure. Take a backup first (`docker exec <backup task> sh /scripts/backup.sh`).
- Rollback of code: redeploy the previous `TEMPO_VERSION`. Migrations are forward-only in
  production: restore the pre-update backup if a schema change must be undone.
- Token rotation (D023): create `tempo_api_token_v2`, deploy it as `tempo_api_token` with the old
  one as `tempo_api_token_previous`, update HA, then remove the previous secret.

### 4. Operations
- Health: `api` and `rt-relay` health checks; `job_run` table records each worker job (HA sensors
  expose plan status and issues).
- Backups: daily custom-format dump in the `backups` volume (14 days); copy them off the node.
- Restore drill (monthly): `docker exec <backup task> sh /scripts/restore_drill.sh` restores the
  newest dump into a scratch database, compares row counts and drops it.
- Data retention: daily worker job (notifications 90 d, decided proposals/activities 180 d,
  superseded plans 30 d). `audit_entry` is pruned only with the owner role, if ever needed.
