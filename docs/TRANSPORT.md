# Transport and location (milestone 4)

Code: `backend/src/tempo/transport/`.

| Module | Role |
|---|---|
| `places.py` | Named places with coordinates; exact (case/space-insensitive) alias resolution of iCal `LOCATION` text. Unknown text ⇒ `None` ⇒ ask the user. |
| `location.py` | HA phone tracker position with freshness (≤ 10 min, not in the future) and accuracy (≤ 200 m) checks; origin inference for future trips. |
| `routing.py` | `Journey`/`Leg` types (realtime flag, delay, cancellation) and the `RoutingProvider` protocol; `RoutingUnavailableError`. |
| `motis.py` | `MotisClient` for a private MOTIS (`GET /api/v6/plan`, `arriveBy`), using `httpx2`. |
| `travel.py` | Mandatory travel between located commitments and travel-aware study windows; live-delay and disruption warnings. |
| `feeds.py` | Registry of the 10 Belgian feeds (verified URLs) and portal quota budget per access tier. |
| `gtfsrt.py` | GTFS-RT decoding, validation (decodable, fresh ≤ 10 min) and TEC repair. |
| `rt_relay.py` | Internal relay portal → MOTIS: key holder, quota guard, back-off, repair, 503 on stale data. |

## Origin rules (REQUIREMENTS: position and routing)
1. Departure in the past ⇒ `NEEDS_CLARIFICATION`.
2. Departure within 2 h and phone position FRESH ⇒ current position (the tracker is the source of
   the actual location).
3. Otherwise the most recent known fact before departure: the preceding event of the same local
   day (as of its end) or a declared stay covering the departure (as of its start).
   Several overlapping stays ⇒ `NEEDS_CLARIFICATION`.
4. Otherwise `NEEDS_CLARIFICATION` with a reason. Today's GPS is never used for a later day and
   there is no automatic switching between family homes.

## Travel rules
- Outbound: arrive `buffer` (default 10 min) before the event; block = [departure, event start].
- Return: block = [event end, arrival]; no margin (no deadline).
- Latest feasible departure wins, then fewest transfers; cancelled itineraries are discarded.
- No feasible journey ⇒ `IMPOSSIBLE`; routing outage ⇒ `UNKNOWN`. Both are reported and the gap
  is blocked. A missing return blocks 2 h after the last event (conservative, labelled, not a
  journey). A missing start place is reported (outbound trip not planned). Nothing is invented.
- `Journey` rejects inconsistent itineraries (arrival before departure, overlapping legs, legs
  outside bounds) so a bad answer cannot hide a mandatory trip.
- Transit legs without live data are exposed as `static_transit_legs` (STIB: "static timetable
  only").
- Study windows tagged with a place are reduced by travel to/from every away commitment within
  6 h (`MAX_TRAVEL`); a short gap between two away events is therefore not studyable at home.
  Missing journeys block only the affected side of the window and are reported.
- Adversarial review by agent `transport`: 9 findings + static label fixed with regression tests
  (`tests/test_transport_regressions.py`). Known gaps: no MOTIS pagination (`nextPageCursor`),
  one malformed leg invalidates the whole answer (safe but strict).
- All travel blocks become `busy` intervals of the CP-SAT planner (end-to-end test in
  `tests/test_travel.py`).

## Verification status — read carefully
- **Verified against a REAL MOTIS v2.11.4** (local Docker, Brussels region: STIB + SNCB GTFS of
  2026-11-23, SNCB GTFS-RT, BBBike OSM — see `routing/README.md`):
  `tests/integration/test_motis_live.py` (health, arrive-by deadlines, depart-at, STIB never
  realtime, out-of-region ⇒ no journey, out-of-timetable ⇒ error with MOTIS' reason, full day of
  travel routed end to end). A real response is recorded in
  `tests/fixtures/motis_plan_v6_real_brussels.json` and parsed offline.
- Observed: SNCB GTFS-RT applied (163/164 updates resolved); SNCB legs report `realTime=true`
  for running trips; STIB legs are static only, as expected. No delay was observed during the
  session, so **delay handling is verified only on synthetic data**.
- **Nationwide verified (2026-11-23)**: import + server + 4 operators routed
  (`tests/integration/test_motis_belgium_live.py`, 7 passed), full chain portal → relay → MOTIS
  with all 6 realtime feeds in anonymous mode (daily interval, 6 requests). A real **+7 min** live
  delay (IC Gent → Brugge) was observed through `MotisClient`. Measures in `routing/README.md`.
- **NOT verified yet:** Standard key acceptance by the portal host (no key yet), long-running
  refresh (hours/days), Swarm deployment, portal gzip support.
- Timestamps emitted by MOTIS are RFC 3339 with `Z`; parsed strictly.

## Belgian feeds (10) — milestone M4c

Verified 2026-11-23 by real HTTP requests (anonymous, HTTP 200). Registry: `tempo.transport.feeds.FEEDS`.
Base `https://api-management-discovery-production.azure-api.net/api/gtfs/feed/<operator>/...`,
operators `tec`, `nmbssncb`, `delijn`, `stibmivb`.

| # | Feed | Path | Format | Publisher refresh | Notes |
|---|---|---|---|---|---|
| 1 | TEC GTFS static | `tec/static` | ZIP 97 MB | daily | no `frequencies.txt` |
| 2 | SNCB/NMBS GTFS static | `nmbssncb/static` | ZIP 7.5 MB | daily | |
| 3 | De Lijn GTFS static | `delijn/static` | ZIP 184 MB | daily | |
| 4 | STIB/MIVB GTFS static | `stibmivb/static` | ZIP 14 MB | daily | STIB has no GTFS-RT |
| 5 | TEC TripUpdates | `tec/rt/trip-update?format=protobuf` | GTFS-RT 1.0, 1.7 MB | 30 s | **repaired** (see below) |
| 6 | SNCB TripUpdates | `nmbssncb/rt/trip-update?format=protobuf` | GTFS-RT 2.0, small | 30 s | 90 % resolved |
| 7 | De Lijn TripUpdates | `delijn/rt/trip-update?format=protobuf` | GTFS-RT 2.0, 4.6 MB | 30 s | 97.5 % resolved |
| 8 | TEC Alerts | `tec/rt/alert?format=protobuf` | GTFS-RT 1.0, 8.7 MB | 30 s | 11 008 alerts; repaired |
| 9 | SNCB Alerts | `nmbssncb/rt/alert?format=protobuf` | GTFS-RT 2.0, 94 kB | 30 s | agency-wide, no period |
| 10 | De Lijn Alerts | `delijn/rt/alert?format=protobuf` | GTFS-RT 2.0, 0.8 MB | 30 s | |

- Realtime is JSON by default on this host: `?format=protobuf` is mandatory.
- The portal also documents `opendata-discovery-gtfs-realtime.api.production.belgianmobility.io/api/gtfs-rt/<agency>/TripUpdates.pbf`
  and `.../ServiceAlerts.pbf`; that host had **no public DNS record** on 2026-11-23 → not used.
- Authentication: anonymous works; Standard key in header `bmc-partner-key` (portal knowledge base).
  Acceptance of the key by this host is **unverified** until the user creates an account.
- Quotas (terms §5, indicative): anonymous 100/day, 10/min; Standard 12 000/day, 500/min. Tempo
  plans at most 80 %. Default relay plan with a key: trip updates every 2 min, alerts every 10 min
  = 2 596 requests/day (≈ 6 GB/day of downloads). Anonymous: SNCB trip updates every 30 min only.
- Static feeds: `routing/fetch_data.py` (one request each), then MOTIS import.

### TEC repair (D021)
TEC trip descriptors carry `start_time` shifted by 12 h for afternoon trips (trip departing 20:05
announced `08:05:00`); MOTIS resolved only 50 % of TEC trip updates and 76 % of TEC alert
selectors. TEC has no `frequencies.txt`, and the GTFS-RT reference allows omitting `start_time` for
non-frequency trips, so the relay removes it for SCHEDULED/CANCELED trips identified by `trip_id`
(ADDED/NEW/DUPLICATED trips keep it). `routing/fetch_data.py` fails loudly if TEC ever publishes a
`frequencies.txt`. Live result: 100 % /
100 %. Delays, cancellations and stop updates are untouched (tested on real extracts).

### Alerts policy
MOTIS attaches alerts to legs, including network-wide notices and notices outside their impact
period (strike of 23 Nov attached to legs of 24 Nov). Tempo therefore:
- keeps only alerts whose impact period overlaps the leg (`Leg.relevant_alerts`);
- never drops a journey because of an alert (it would wrongly make mandatory travel impossible);
  cancellations and delays come from trip updates;
- uses timed severe alerts only as a tie-break after departure time;
- warns individually about timed severe alerts and live delays ≥ 2 min (departure or arrival);
  severe notices without a period (all SNCB notices) are summarised once per plan as "not checked
  against the planned journeys"; distinct alerts are deduplicated on all their fields.

### Relay (`python -m tempo.transport.rt_relay`, port 8090, internal network only)
`GET /feeds/<operator>/<trip-updates|alerts>.pbf`, `GET /health` (quota usage, feed ages; no key).
Settings: `TEMPO_RELAY_BMC_PARTNER_KEY` (Docker secret `tempo_relay_bmc_partner_key`),
`TEMPO_RELAY_TRIP_UPDATES_SECONDS`, `TEMPO_RELAY_ALERTS_SECONDS`, `TEMPO_RELAY_FEEDS`
(`tec/trip-updates,...`), `TEMPO_RELAY_STATE_FILE` (request log on a persistent volume). The quota
budget is checked at start (refuses to start if over 80 %); a rolling 24 h request log (persisted,
corrupt file ⇒ refuse to start) blocks further portal calls and logs once when exhausted; HTTP 429
waits at least `Retry-After` (never shortened) and at least 5 min; other failures back off 5 min ×
failures up to 1 h; a copy older than 10 min is answered 503. Run **one replica only**.
- Verified live: when the relay answers 503, MOTIS (`incremental_rt_update: false`) drops the
  realtime data of that feed at its next update (predicted stop times 2/300 → 0/300), so stale
  delays are not presented as live.
- Anonymous mode (SNCB trip updates every 30 min, freshness limit 10 min) gives live data only
  about a third of the time — honest but limited; a Standard key is needed for useful realtime.

## Departure re-check (M4d) — `departure.py`
`check_departure(event, phone_position, provider, now=..., buffer=10 min, planned_departure=None)`
answers "can I still make it from where I am now, and when must I leave?" with live routing:

| Status | Meaning |
|---|---|
| `unknown_position` | phone position missing, stale (> 10 min) or inaccurate (> 200 m): never guessed |
| `already_there` | within 150 m of the event place |
| `on_time` / `leave_now` | latest journey keeping the buffer; `leave_now` if it leaves within 5 min |
| `leave_earlier` | that journey leaves before the previously planned departure (e.g. live delay) |
| `at_risk` | no journey keeps the buffer, earliest arrival is still before the start |
| `late` | earliest arrival after the start (`minutes_late`), or event started elsewhere |
| `impossible` | no journey at all from here |
| `unknown_routing` | routing outage (never reported as impossible) |

Journeys leaving before `now` are ignored. Warnings: live delays ≥ 2 min, timed severe notices,
static-only legs. Scheduling the checks (planning time, evening before, near departure) and the HA
notifications belong to the worker (M7); not wired yet.

## STIB realtime (milestone 13) — `stib.py`
Verified 2026-11-23 by anonymous requests (HTTP 200, JSON): the Belgian Mobility portal serves
STIB snapshots at `https://api-management-discovery-production.azure-api.net/api/datasets/stibmivb/`
`rt/WaitingTimes` (next passages per stop and line, ISO times with offset, optional message) and
`rt/TravellersInformation` (free-text disruptions with lines/points); `rt/VehiclePositions` exists
(stop + distance, no GPS) and is not used. The old `opendata-api.stib-mivb.be` / Opendatasoft
portals redirect or are gone. STIB still has **no GTFS-RT**: MOTIS cannot use STIB realtime.

- Stop mapping verified on a real snapshot: every `pointid` (940/940) equals a STIB GTFS `stop_id`
  without its platform letter (`0470F` -> `0470`); MOTIS stop ids are `stib_<stop_id>`.
- Use in Tempo: during a departure check, when the journey boards a STIB line within 60 min, the
  worker adds "STIB temps réel : ligne 59 vers X, passage prévu à HH:MM", the line message
  (e.g. "Ligne déviée") and matching traveller information to the advice/notification.
  Future plans keep the static STIB timetable (no trip ids, no delays in these feeds).
- Quota: on-demand only, cached 15 min, capped at 20 requests/24 h anonymous (2 000 with the key
  secret `tempo_bmc_partner_key`; key acceptance on this host still unverified).
- Fixtures: `backend/tests/fixtures/stib/*.json` (real snapshot extracts, CC BY 4.0).

## Autonomous routing data (milestone 13) — `gtfs_update.py` + `routing/scripts/supervisor.sh`
- **Refresh** (`routing-update` service, nightly): conditional download with the stored ETag —
  verified 2026-11-23 that the portal answers `304` with an empty body when a feed is unchanged
  (saves ~300 MB/day: TEC 97 MB, De Lijn 184 MB, STIB 14 MB, SNCB 7.5 MB) and that Geofabrik
  publishes ETags (OSM 700 MB, refreshed weekly; URL moved to
  `/europe/belgium/belgium-latest.osm.pbf`). Each GTFS is validated (zip integrity, required
  files, calendar covering at least 7 more days, size not below 50 % of the previous copy);
  rejected downloads leave the previous file in place. `READY` changes only when a file changed.
- **Rebuild** (`routing` service entrypoint): imports the new version into `/data/build-<v>`,
  switches the `current` symlink atomically only if the import succeeds, restarts MOTIS
  (~1-2 min of routing outage, reported as "routing unavailable" and retried by the worker),
  keeps the previous graph for rollback, never retries a failed version until a newer one comes,
  and restarts MOTIS if it crashes.
- **Verified for real** (2026-11-23, MOTIS v2.11.4, Brussels data, throwaway volumes): first import
  + serve (health 200 after ~12 s), new version -> re-import + switch, corrupted STIB GTFS ->
  import failed, previous graph kept serving (health 200), no retry loop, killed MOTIS restarted,
  graceful stop.
- **Bandwidth summary**: static feeds cost a few 304s/day when unchanged; realtime goes through the
  relay at its plan (default 2 min TU / 10 min alerts with a key); STIB snapshots on demand only.
