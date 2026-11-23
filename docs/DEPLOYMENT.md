# Deployment strategy (NOT YET EXECUTABLE)

Target: existing Debian Docker Swarm managed by Portainer, with Home Assistant already deployed. No public Tempo API. Prefer internal network and service credentials; protect DB and GTFS volumes, limit capabilities, verify container images, avoid mounting Docker socket. Use Docker secrets or an equivalent external secret manager. Never place HA tokens, email credentials, LLM keys or location history in Git.

Before production: verify resources for Belgian graph and MOTIS, test HA connection, test external feed terms, configure backups, health checks, rate limits, rolling upgrade/rollback, disaster recovery. Require explicit deployment approval. `deploy/` is intentionally empty until feasibility is validated.
