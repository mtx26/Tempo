# Transport and location (milestone 4)

Code: `backend/src/tempo/transport/`.

| Module | Role |
|---|---|
| `places.py` | Named places with coordinates; exact (case/space-insensitive) alias resolution of iCal `LOCATION` text. Unknown text ⇒ `None` ⇒ ask the user. |
| `location.py` | HA phone tracker position with freshness (≤ 10 min, not in the future) and accuracy (≤ 200 m) checks; origin inference for future trips. |
| `routing.py` | `Journey`/`Leg` types (realtime flag, delay, cancellation) and the `RoutingProvider` protocol; `RoutingUnavailableError`. |
| `motis.py` | `MotisClient` for a private MOTIS (`GET /api/v6/plan`, `arriveBy`), using `httpx2`. |
| `travel.py` | Mandatory travel between located commitments and travel-aware study windows. |

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
- **NOT verified yet:** nationwide data (config prepared in `routing/config/belgium.yml`), TEC and
  De Lijn realtime (need a Standard account key), Swarm deployment, long-running feed refresh.
- Timestamps emitted by MOTIS are RFC 3339 with `Z`; parsed strictly.
