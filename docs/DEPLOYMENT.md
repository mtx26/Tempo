# Deployment strategy (NOT YET EXECUTABLE)

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
