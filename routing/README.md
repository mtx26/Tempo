# Tempo routing (MOTIS) — local development instance

Private routing service for Tempo Core. **Development only**: nothing here is deployed.

| File | Purpose |
|---|---|
| `Dockerfile` | Official MOTIS release binary, pinned by version **and SHA-256**, non-root user. |
| `fetch_data.py` | Downloads OSM + Belgian GTFS into `data/<region>/` (git-ignored) with a manifest. |
| `config/brussels.yml` | Small test region: STIB + SNCB GTFS, SNCB GTFS-RT, BBBike Brussels OSM. |
| `config/belgium.yml` | Nationwide: STIB, SNCB, TEC, De Lijn GTFS + 6 GTFS-RT feeds through the Tempo relay. |

## Run the Brussels test region (Windows, Docker Desktop, Git Bash)

```bash
export PATH="$LOCALAPPDATA/Programs/DockerDesktop/resources/bin:$PATH"
python routing/fetch_data.py brussels
docker build -t tempo-routing:v2.11.4 routing
W="$(cygpath -w "$PWD/routing")"
MSYS_NO_PATHCONV=1 docker run --rm --memory 6g \
  -v "$W\data\brussels:/input:ro" -v "$W\config\brussels.yml:/config/config.yml:ro" \
  -v tempo-routing-brussels:/data tempo-routing:v2.11.4 import -c /config/config.yml -d /data/brussels
MSYS_NO_PATHCONV=1 docker run -d --name tempo-motis-brussels --memory 4g \
  -p 127.0.0.1:18080:8080 -v tempo-routing-brussels:/data tempo-routing:v2.11.4 server -d /data/brussels
cd backend && TEMPO_TEST_MOTIS_URL=http://127.0.0.1:18080 uv run pytest -q tests/integration/test_motis_live.py
```

The port is published on `127.0.0.1` only. Re-run `fetch_data.py` + `import` when feeds are
refreshed (STIB's GTFS of 2026-11-23 is valid until 2026-12-20; queries outside the loaded window
return HTTP 400, which Tempo reports as routing unavailable with MOTIS' reason).

## Measured (2026-11-23, Brussels region, Docker Desktop on Windows, 12 CPUs)
- Inputs 98 MB (OSM 81 MB, STIB 14 MB, SNCB 7.5 MB); import **20 s**; imported data **182 MB**.
- Server RSS after start ≈ **103 MB**; SNCB GTFS-RT: 163/164 trip updates resolved (99.4 %).

## Nationwide Belgium (benchmarked 2026-11-23, same machine)

```bash
python routing/fetch_data.py belgium      # 957 MB: OSM 700 MB, De Lijn 184 MB, TEC 97 MB, STIB 14 MB, SNCB 7.5 MB
MSYS_NO_PATHCONV=1 docker run --rm --memory 7g \
  -v "$W\data\belgium:/input:ro" -v "$W\config\belgium.yml:/config/config.yml:ro" \
  -v tempo-routing-belgium:/data tempo-routing:v2.11.4 import -c /config/config.yml -d /data/belgium
# Realtime relay (backend). Anonymous: SNCB trip updates only; Standard key: all 6 feeds.
# `python -m tempo.transport.rt_relay` binds 0.0.0.0 (container); on a workstation bind 127.0.0.1:
cd backend && uv run python -c "import uvicorn; from tempo.transport.rt_relay import *; \
  uvicorn.run(create_relay_app(relay_from_settings(RelaySettings())), host='127.0.0.1', port=8090)"
MSYS_NO_PATHCONV=1 docker run -d --name tempo-motis-belgium --memory 6g \
  --add-host tempo-rt-relay:host-gateway -p 127.0.0.1:18081:8080 \
  -v tempo-routing-belgium:/data tempo-routing:v2.11.4 server -d /data/belgium
cd backend && TEMPO_TEST_MOTIS_BE_URL=http://127.0.0.1:18081 uv run pytest -q tests/integration/test_motis_belgium_live.py
```
Never publish port 8090: the relay spends the user's portal quota (see `docs/TRANSPORT.md`).

| Measure | Value |
|---|---|
| Import (osr + timetable + matches + footpaths) | **3 min 29 s**, exit 0, no OOM under a 7 GB limit; peak RAM NOT recorded |
| Imported data | **1.8 GB** |
| Server ready after start | ~2 min (loading) |
| Server RAM | 655 MB idle → 877 MB after 16 queries; 723 MB with all 6 realtime feeds loaded |
| Query latency `/api/v6/plan` (8 nationwide pairs) | cold median 130 ms (max 602 ms), warm median 100 ms (max 248 ms) |
| Operators routed | SNCB, TEC, De Lijn, STIB (e.g. Liège→Gent 97 min direct IC; Bastogne→Liège TEC; Leuven bus 43 De Lijn) |

Realtime resolution in MOTIS (live feeds of 2026-11-23 ~19:05 UTC):

| Feed | Direct from portal | Through Tempo relay |
|---|---|---|
| SNCB trip updates | 90 % (≈19/195 unknown trips) | 90 % |
| SNCB alerts (39, agency-wide, no period) | 100 % | 100 % |
| TEC trip updates | **50 %** (start_time shifted 12 h) | **100 %** |
| TEC alerts (11 008) | 76 % | **100 %** |
| De Lijn trip updates | 97.5 % | 97.5 % |
| De Lijn alerts | 99.99 % | 99.99 % |

Not measured: peak import RAM, long-running feed refresh, behaviour on the Swarm node (gate before
production). Sizes per poll: De Lijn TU 4.6 MB, TEC alerts 8.7 MB, TEC TU 1.7 MB — at the default
relay intervals this is roughly 6 GB/day of downloads (TU 6.4 MB x 720 + alerts 9.6 MB x 144); whether the portal serves gzip is unverified.

Licences/attribution: © OpenStreetMap contributors (ODbL); STIB, NMBS/SNCB, TEC, De Lijn —
Open Data via Belgian Mobility (CC BY 4.0, "Source: <operator> – Open Data – <date>").
