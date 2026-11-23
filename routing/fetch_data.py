"""Download routing input data for a local MOTIS instance (development only).

Usage (from the repository root):

    python routing/fetch_data.py brussels      # small test region (~100 MB)
    python routing/fetch_data.py belgium       # nationwide preparation (~800 MB)

Files go to ``routing/data/<region>/`` (git-ignored). A ``manifest.json`` records URL, size,
SHA-256 and download time of each file, so results are reproducible and auditable.

Licences: OpenStreetMap data (c) OpenStreetMap contributors, ODbL. Belgian GTFS feeds are
published by each operator under CC BY 4.0 through the Belgian Mobility Open Data portal
("Source: <operator> - Open Data - <date>"). Anonymous portal quota: 100 requests/day,
10/minute — this script makes one request per GTFS feed.
"""

import hashlib
import json
import sys
import time
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

BMC = "https://api-management-discovery-production.azure-api.net/api/gtfs/feed/{}/static"
REGIONS: dict[str, dict[str, str]] = {
    "brussels": {
        "osm.pbf": "https://download.bbbike.org/osm/bbbike/Bruessel/Bruessel.osm.pbf",
        "stib.gtfs.zip": BMC.format("stibmivb"),
        "sncb.gtfs.zip": BMC.format("nmbssncb"),
    },
    "belgium": {
        "osm.pbf": "https://download.geofabrik.de/europe/belgium-latest.osm.pbf",
        "stib.gtfs.zip": BMC.format("stibmivb"),
        "sncb.gtfs.zip": BMC.format("nmbssncb"),
        "tec.gtfs.zip": BMC.format("tec"),
        "delijn.gtfs.zip": BMC.format("delijn"),
    },
}
USER_AGENT = "tempo-dev-routing-fetch/0.1"
REPAIRED_FEEDS = ("tec.gtfs.zip",)
"""Feeds whose realtime is repaired by dropping start_time (backend tempo.transport.gtfsrt, D021):
only valid while their static GTFS has no frequencies.txt."""
CHUNK = 1 << 20


def download(url: str, target: Path) -> dict[str, object]:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    digest = hashlib.sha256()
    size = 0
    partial = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as out:  # noqa: S310
        while chunk := response.read(CHUNK):
            out.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    partial.replace(target)
    return {
        "url": url,
        "bytes": size,
        "sha256": digest.hexdigest(),
        "downloaded_at": datetime.now(UTC).isoformat(),
    }


def main(region: str) -> None:
    if region not in REGIONS:
        raise SystemExit(f"unknown region {region!r}; choose from {sorted(REGIONS)}")
    folder = Path(__file__).parent / "data" / region
    folder.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {}
    for name, url in REGIONS[region].items():
        print(f"downloading {name} ...", flush=True)
        manifest[name] = download(url, folder / name)
        time.sleep(7)  # stay well below the anonymous 10 requests/minute limit
    for name in REPAIRED_FEEDS:
        if name in manifest and "frequencies.txt" in zipfile.ZipFile(folder / name).namelist():
            raise SystemExit(
                f"{name} now contains frequencies.txt: the TEC start_time repair (D021) is no "
                "longer valid. Disable it (feeds.py needs_repair) and re-check TEC resolution."
            )
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "brussels")
