# Tempo architecture

## System
Phone/Desktop → existing Home Assistant (single UI, existing remote access) → custom `custom_components/tempo` integration (HACS-compatible) → private Tempo FastAPI service → PostgreSQL, OR-Tools CP-SAT, worker, MOTIS. No separate website, public backend URL, or second user login. Internal service authentication still required.

## Services
- `tempo-api`: FastAPI, validation, domain rules, proposal/approval API, model tool orchestration.
- `tempo-worker`: scheduled iCal/email/transport sync, recalculation, notifications and recovery.
- `tempo-postgres`: authoritative durable store, versioned migrations and audit log.
- `tempo-routing`: MOTIS + OpenStreetMap + validated Belgian GTFS/NeTEx feeds and realtime adapters.
- `homeassistant/custom_components/tempo`: config flow, coordinator, calendar, todo, sensors, conversation integration and actions (implement incrementally; do not claim entities exist yet).

## Sources
- HA configured phone tracker for actual position, freshness and accuracy checks; future origin derives from preceding event or explicit future location when GPS alone is unsuitable.
- University ICS/iCal for FIXED timetable; stable IDs, cancellations, time zones, sync history.
- Only explicitly authorized sender/thread for sport emails; content preserved.
- Belgian Mobility Open Data for TEC, SNCB, STIB, De Lijn static and realtime where available. Verify feed access, quotas, licenses, data quality, SIRI/GTFS-RT support and STIB adapters before implementation.
- OSM for pedestrian routing; MOTIS as candidate, OpenTripPlanner fallback after benchmark.

## Domain model
Tasks (subject, effort estimate, remaining effort, target date with hard/soft flag, priority), movable study sessions, fixed events, flexible training, fixed races, availability, locations, journeys, schedule versions, change proposals, preferences, sync state, audit entries.

## Control plane
AI interprets natural language, never decides feasibility alone. Deterministic rules validate, OR-Tools optimizes; MOTIS provides route options; HA displays and approves protected changes. Worker runs without active chat or Claude Code.

## Security and reliability
Swarm internal networks, authenticated internal APIs, least privilege, no Docker socket mounts unless separately justified, secrets outside Git, PostgreSQL backups and restore drills, structured sanitized logs, health checks, rate-limited external polling, idempotent sync, timezone-safe scheduling, migrations and integration tests.
