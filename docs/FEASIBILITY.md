# Phase 0 — Environment and feasibility report

Date: 2026-11-23. Sources were read on this date; **V** = verified from a primary source,
**A** = assumption / not yet verified, **R** = risk.

## 1. Local development environment (V, measured)

| Item | Result |
|---|---|
| OS | Windows 11 Pro, 12 logical CPUs, 15.7 GB RAM |
| Disk | C: 32 GB free of 476 GB (94 % used) — **R**: too tight for a Belgian MOTIS import + Docker images |
| Python | 3.13.1 (python.org) and 3.14.3 (msys2); `uv` 0.13.9 |
| Node | 22.13.1 / npm 10.9.2 (not needed by Tempo) |
| Git | 2.56.1; the project directory is **not** a git repository yet |
| Docker | **absent** (only a `Docker.backup` folder; WSL distro `docker-desktop` stopped, no `docker` CLI) |
| WSL | `Ubuntu` (WSL2) present, stopped |
| Dependency probe | `uv add ortools fastapi pydantic-settings icalendar` succeeded: ortools 9.15.6755, fastapi 0.165.0, icalendar 7.3.2 |

Consequences: unit tests, lint and type checks run locally. PostgreSQL integration tests and
MOTIS benchmarks cannot run locally until Docker (or a Postgres install / WSL) is available; they
are written to **skip explicitly**, never to pass silently.

## 2. Claude Code tooling (V)

- Superpowers 7.0.2 enabled (brainstorming, writing-plans, TDD, systematic-debugging, code review…).
- Context7 1.0.4 enabled but its MCP server exposes only `authenticate` tools → **not usable until
  the user authenticates**. Documentation was read from primary web sources instead.
- Project agents (6): architect, integrations, scheduler, security, tester, transport. They have
  Read/Grep/Glob/Bash only (no web access, no Write/Edit tools).
- Project skills: 4 Tempo-specific (`tempo-schedule`, `tempo-transport`, `tempo-integrations`,
  `tempo-deploy`) + 3 generic (`implement-feature`, `debug`, `review`).
- `.claude/settings.json` denies `rm -rf`, `git push`, `docker stack deploy`, `ssh`.

## 3. MOTIS (V: github.com/motis-project/motis README, releases, openapi.yaml, docs/setup.md)

- Latest release **v2.11.4** (21 Oct; year not shown on page, A: 2026). MIT licence.
- Inputs: one OSM `osm.pbf`; multiple **GTFS and NeTEx** static feeds (incl. GTFS-Flex, Fares v2);
  real-time **GTFS-RT, SIRI-ET, SIRI-SX, SIRI-FM, VDV 454**. SIRI is "SIRI Lite … still work in progress".
- Workflow: `motis config my.osm.pbf gtfs.zip` → `motis import` → `motis server` (port 8080).
  Linux binaries `motis-linux-amd64.tar.bz2`; a `Dockerfile` exists in the repo (A: no confirmed
  official ghcr image).
- API: OpenAPI `v6`, `GET /api/v6/plan`, **`arriveBy=true`** supported (fix to arriveBy search
  window in v2.11.3). Pedestrian profile `FOOT`, street routing must be enabled (`street_routing`).
- Resources: **no numeric RAM/disk figures published** ("low memory usage", planet-scale capable).
  **A/R**: Belgium OSM extract + 4 GTFS feeds must be benchmarked on the Swarm node before
  committing. Tempo pins the API version (`/api/v6`) behind an adapter interface.

## 4. Belgian Mobility Open Data (V: data.belgianmobility.io data, index, terms, FAQ, knowledge base)

| Operator | GTFS static | GTFS-RT | SIRI (BMC-generated) | NeTEx EPIP | Other |
|---|---|---|---|---|---|
| NMBS-SNCB | daily | TU, SA | ET, SX | daily | — |
| LETEC (TEC) | daily | TU, SA, VP | ET, SX, VM | daily | — |
| De Lijn | daily | TU, SA | ET, SX | daily | — |
| STIB-MIVB | daily | **none** | **none** | daily | JSON APIs: waiting times, vehicle positions (15 s), traveller info |

- Licence: **CC BY 4.0**, operator is licensor; attribution format
  "Source: [PTO] – Open Data – [date]". Data "as is", no SLA for Anonymous/Standard tiers.
- Quotas (terms §5): **Anonymous 100 req/day, 10/min** (insufficient for polling);
  **Standard (free account) 12 000/day, 500/min**; Gold on request. HTTP 429 when exceeded.
  Key sent in header `bmc-partner-key`.
- **Polling budget (computed)**: TU for 3 operators every 60 s = 4 320 req/day + SA every 5 min
  = 864 → ≈ 5 200/day, within Standard. Static feeds once a day. Requires a **free account
  created by the user** (Tempo will not create accounts).
- **R — STIB realtime**: no GTFS-RT/SIRI, so MOTIS cannot consume STIB live data directly. Options:
  (a) static-only STIB routing with an explicit "no live data" flag (MVP), (b) later a
  Tempo-side check of STIB waiting times near departure, (c) a converter to GTFS-RT (complex).
- **A**: whether NeTEx and GTFS combined in MOTIS beats GTFS-only for Belgium; start GTFS-only.

## 5. Home Assistant / HACS (V: hacs.xyz publish pages, contributors.home-assistant.io manifest page)

- HACS page states HACS is built into Home Assistant as "Marketplace" starting with 2026.11.
- Repository: public GitHub repo; **one integration per repo**, files under
  `ROOT/custom_components/<domain>/`; `hacs.json` at repo root with at least `name`
  (optional `homeassistant` minimum version, `zip_release`, `persistent_directory`…); README,
  description, topics, GitHub releases (not just tags); brand assets (`brand/icon.png`).
- `manifest.json` (custom): `domain`, `name`, `codeowners`, `documentation`, `issue_tracker`,
  `version` (required); `iot_class` (here `local_polling`), `integration_type` (`service`),
  `config_flow: true`. Hassfest key order: domain, name, then alphabetical.
- Validation: `hacs/action` (category `integration`) + hassfest action in CI.
- **R / decision for user**: the current layout `homeassistant/custom_components/tempo/` is **not**
  HACS-installable at a monorepo root. Options: (1) dedicated public repo for the integration
  (recommended: backend can stay private), (2) move `custom_components/` to the root of this repo,
  which then must be public. Recorded as open decision D010.

## 6. Feasibility verdict

Technically feasible with these gates:
1. User creates a free Belgian Mobility account (Standard tier) before live polling.
2. MOTIS Belgium import benchmarked on the Swarm node (RAM, disk, import time, query latency).
   Done on the dev PC on 2026-11-23 (import 3 min 29 s, 1.8 GB data, ~0.7–0.9 GB server RAM, median
   query 100–130 ms; routing/README.md); still to repeat on the Swarm node before production.
3. STIB realtime explicitly unsupported in MVP; Tempo reports "static timetable only" for STIB legs.
4. Decide HACS repository layout (D010) and LLM provider before the corresponding milestones.
