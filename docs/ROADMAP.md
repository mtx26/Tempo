# Roadmap (gated milestones)

0. **Environment and feasibility**: inspect HA APIs, MOTIS versions, Belgian Mobility feed auth/quotas and actual supported realtime; estimate RAM/CPU; document decisions and risks. No deployment.
1. **Backend foundation**: FastAPI, typed config, Postgres, Alembic, Docker development stack, health checks, pytest, lint/typecheck, CI.
2. **Domain and sync**: tasks, soft/hard target dates, ICS fixed events, availability, idempotent imports, timezone correctness.
3. **Scheduler**: OR-Tools hard/soft constraints, 90-min preferences, subject alternation, remaining work, versioning and simulations.
4. **Transport**: Belgian feed ingestion, MOTIS integration, walking and transfer buffers, GPS tracker, future origins, route validation and fallback.
5. **Sports**: narrowly scoped authorized email ingestion, flexible training, fixed races.
6. **Home Assistant custom integration**: config flow, calendar, todo, sensors, services, conversation, notifications, approvals; HACS distribution validation.
7. **Continuous operations**: workers, live disruption alerts, approval workflow, monitoring, backups and failure recovery.
8. **Production readiness**: end-to-end simulations, security audit, resource benchmark, restore drill, deployment plan; request user approval before deploy.

Each milestone: tests + lint + documentation + PROGRESS.md + explicit report of gaps. Never skip a failed gate.
