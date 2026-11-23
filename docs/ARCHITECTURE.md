# Tempo architecture

## System
Phone/Desktop → existing Home Assistant (single UI, existing remote access) → custom `custom_components/tempo` integration (HACS-compatible) → private Tempo FastAPI service → PostgreSQL, OR-Tools CP-SAT, worker, MOTIS. No separate website, public backend URL, or second user login. Internal service authentication still required.

## Services
- `tempo-api`: FastAPI, validation, domain rules, proposal/approval API, model tool orchestration.
- `tempo-worker`: scheduled iCal/email/transport sync, recalculation, notifications and recovery.
- `tempo-postgres`: authoritative durable store, versioned migrations and audit log.
- `tempo-routing`: MOTIS + OpenStreetMap + Belgian GTFS static (TEC, SNCB, De Lijn, STIB).
- `tempo-rt-relay`: internal GTFS-RT relay (TripUpdates + Alerts of TEC, SNCB, De Lijn) between the Belgian Mobility portal and MOTIS: holds the portal key secret, enforces quota, validates freshness, repairs TEC (D021). Internal network only.
- `custom_components/tempo` in the separate public-ready repository `tempo-homeassistant` (D010): config flow, coordinator, calendar, todo, sensors, conversation integration and actions (implement incrementally; do not claim entities exist yet).

## Sources
- HA configured phone tracker for actual position, freshness and accuracy checks; future origin derives from preceding event or explicit future location when GPS alone is unsuitable.
- University ICS/iCal for FIXED timetable; stable IDs, cancellations, time zones, sync history.
- Only explicitly authorized sender/thread for sport emails; content preserved.
- Belgian Mobility Open Data: 4 GTFS static + 3 TripUpdates + 3 Alerts feeds, verified 2026-11-23 (docs/TRANSPORT.md "Belgian feeds"). STIB has no realtime (static only; optional waiting-times adapter later).
- OSM for pedestrian routing; MOTIS as candidate, OpenTripPlanner fallback after benchmark.

## Domain model
Tasks (subject, effort estimate, remaining effort, target date with hard/soft flag, priority), movable study sessions, fixed events, flexible training, fixed races, availability, locations, journeys, schedule versions, change proposals, preferences, sync state, audit entries.

## Control plane
AI interprets natural language, never decides feasibility alone. Deterministic rules validate, OR-Tools optimizes; MOTIS provides route options; HA displays and approves protected changes. Worker runs without active chat or Claude Code.

## Security and reliability
Swarm internal networks, authenticated internal APIs, least privilege, no Docker socket mounts unless separately justified, secrets outside Git, PostgreSQL backups and restore drills, structured sanitized logs, health checks, rate-limited external polling, idempotent sync, timezone-safe scheduling, migrations and integration tests.
