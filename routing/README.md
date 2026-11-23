# Tempo routing (MOTIS) — local development instance

Private routing service for Tempo Core. **Development only**: nothing here is deployed.

| File | Purpose |
|---|---|
| `Dockerfile` | Official MOTIS release binary, pinned by version **and SHA-256**, non-root user. |
| `fetch_data.py` | Downloads OSM + Belgian GTFS into `data/<region>/` (git-ignored) with a manifest. |
| `config/brussels.yml` | Small test region: STIB + SNCB GTFS, SNCB GTFS-RT, BBBike Brussels OSM. |
| `config/belgium.yml` | Nationwide preparation: STIB, SNCB, TEC, De Lijn GTFS + SNCB GTFS-RT. |

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

Licences/attribution: © OpenStreetMap contributors (ODbL); STIB, NMBS/SNCB, TEC, De Lijn —
Open Data via Belgian Mobility (CC BY 4.0, "Source: <operator> – Open Data – <date>").
