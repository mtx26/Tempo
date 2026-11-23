# Deployment strategy (NOT YET EXECUTABLE)

Target: existing Debian Docker Swarm managed by Portainer, with Home Assistant already deployed. No public Tempo API. Prefer internal network and service credentials; protect DB and GTFS volumes, limit capabilities, verify container images, avoid mounting Docker socket. Use Docker secrets or an equivalent external secret manager. Never place HA tokens, email credentials, LLM keys or location history in Git.

Before production: verify resources for Belgian graph and MOTIS, test HA connection, test external feed terms, configure backups, health checks, rate limits, rolling upgrade/rollback, disaster recovery. Require explicit deployment approval. `deploy/` is intentionally empty until feasibility is validated.

## Database requirements gathered so far (not yet executable)
- PostgreSQL >= 15 (unique constraint with `NULLS NOT DISTINCT`); dev/test uses 16.
- Two roles: a migration role (DDL) used by a one-shot migration job, and a runtime role with only DML
  rights; `audit_entry` limited to INSERT/SELECT for the runtime role (append-only audit).
- A failed sync must be recorded in a separate transaction by the worker (milestone 7).
