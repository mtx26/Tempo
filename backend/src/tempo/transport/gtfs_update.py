"""Automatic refresh of the routing inputs: Belgian GTFS + OpenStreetMap.

Runs as a one-shot job (``python -m tempo.transport.gtfs_update /routing-input``, nightly in the
stack) writing into the volume shared with the routing service:
- conditional downloads with the stored ETag (``If-None-Match``): verified 2026-11-23 that the
  Belgian Mobility portal answers ``304`` with an empty body for an unchanged feed, and that
  Geofabrik publishes ETags — unchanged feeds cost no bandwidth;
- every GTFS is validated before use (zip integrity, required files, calendar still covering the
  coming week, size not collapsing vs the previous copy); an invalid download is discarded and
  the previous file stays in place (reported, never used);
- files are replaced atomically; ``READY`` is rewritten with a new version only when something
  changed, which the routing supervisor (``routing/scripts/supervisor.sh``) turns into a MOTIS
  re-import + switch-over, keeping the previous graph on failure.
"""

import csv
import hashlib
import io
import json
import logging
import sys
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx2

from tempo.transport.feeds import (
    FEEDS,
    HOST,
    KEY_HEADER,
    KEY_SECRET,
    FeedKind,
    MissingKeyError,
    Operator,
)

logger = logging.getLogger(__name__)

OSM_URL = "https://download.geofabrik.de/europe/belgium/belgium-latest.osm.pbf"
OSM_REFRESH = timedelta(days=7)
REQUIRED_GTFS = ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt")
MIN_SIZE_RATIO = 0.5
MAX_DOWNLOAD = 2_000_000_000
COVERAGE = timedelta(days=7)


class InvalidGtfsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    """File name in the input directory (e.g. ``tec.gtfs.zip``)."""
    url: str
    gtfs: bool
    min_interval: timedelta = timedelta(0)
    forbid_frequencies: bool = False
    """The operator's realtime is repaired by dropping ``start_time`` (``gtfsrt``): only valid
    while its static GTFS has no ``frequencies.txt``."""


REGIONS: dict[str, tuple[frozenset[Operator], str]] = {
    "belgium": (frozenset(Operator), OSM_URL),
    # Small development region (MOTIS test instance): STIB + SNCB over the Brussels extract.
    "brussels": (
        frozenset({Operator.STIB, Operator.SNCB}),
        "https://download.bbbike.org/osm/bbbike/Bruessel/Bruessel.osm.pbf",
    ),
}


def sources(region: str = "belgium") -> list[Source]:
    if region not in REGIONS:
        raise ValueError(f"unknown region {region!r}; choose from {sorted(REGIONS)}")
    operators, osm = REGIONS[region]
    repaired = {f.operator for f in FEEDS if f.needs_repair}
    static = [
        Source(
            f"{feed.operator.name.lower().replace('_', '')}.gtfs.zip",
            feed.url,
            True,
            forbid_frequencies=feed.operator in repaired,
        )
        for feed in FEEDS
        if feed.kind is FeedKind.STATIC and feed.operator in operators
    ]
    return [*static, Source("osm.pbf", osm, False, OSM_REFRESH)]


def validate_gtfs(
    data: bytes | Path,
    today: date,
    previous_size: int | None,
    *,
    forbid_frequencies: bool = False,
) -> date | None:
    """Raise ``InvalidGtfsError`` unless the feed is usable; returns its last service date."""
    size = data.stat().st_size if isinstance(data, Path) else len(data)
    if previous_size and size < MIN_SIZE_RATIO * previous_size:
        raise InvalidGtfsError(f"size collapsed ({size} vs {previous_size} bytes)")
    try:
        archive = zipfile.ZipFile(data if isinstance(data, Path) else io.BytesIO(data))
        if archive.testzip() is not None:
            raise InvalidGtfsError("corrupt zip member")
    except zipfile.BadZipFile as exc:
        raise InvalidGtfsError("not a zip file") from exc
    names = set(archive.namelist())
    missing = [n for n in REQUIRED_GTFS if n not in names]
    if missing or not ({"calendar.txt", "calendar_dates.txt"} & names):
        raise InvalidGtfsError(f"missing GTFS files: {missing or ['calendar*.txt']}")
    if forbid_frequencies and "frequencies.txt" in names:
        raise InvalidGtfsError(
            "frequencies.txt present: the realtime start_time repair is no longer valid "
            "(disable needs_repair in transport/feeds.py and re-check realtime resolution)"
        )
    last = _last_service_date(archive, names)
    if last is not None and last < today + COVERAGE:
        raise InvalidGtfsError(f"calendar ends on {last}, less than a week ahead")
    return last


def _last_service_date(archive: zipfile.ZipFile, names: set[str]) -> date | None:
    ends: list[date] = []
    for name, column in (("calendar.txt", "end_date"), ("calendar_dates.txt", "date")):
        if name not in names:
            continue
        with archive.open(name) as handle:
            for row in csv.DictReader(io.TextIOWrapper(handle, "utf-8-sig")):
                value = (row.get(column) or "").strip()
                if len(value) == 8 and value.isdigit():
                    ends.append(date(int(value[:4]), int(value[4:6]), int(value[6:])))
    return max(ends) if ends else None


@dataclass(slots=True)
class UpdateReport:
    changed: list[str]
    unchanged: list[str]
    skipped: list[str]
    failed: dict[str, str]
    version: str | None


def _load(path: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (OSError, ValueError):
        return {}


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


MAX_REDIRECTS = 5
REDIRECTS = frozenset({301, 302, 303, 307, 308})


@dataclass(slots=True)
class _Download:
    status: int
    etag: str | None = None
    size: int = 0
    sha256: str = ""
    error: str | None = None


def _download(
    client: httpx2.Client, url: str, headers: dict[str, str], key: str | None, target: Path
) -> _Download:
    """Stream ``url`` into ``target`` with a size cap. Redirects are followed manually: every hop
    must stay https, and the gateway key is sent ONLY to the gateway host (httpx keeps custom
    headers across hosts, which would leak it to a third-party redirect target)."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme != "https":
            return _Download(0, error="refusing non-https URL")
        hop = dict(headers)
        if key and parts.hostname == HOST:
            hop[KEY_HEADER] = key
        try:
            with client.stream("GET", url, headers=hop, follow_redirects=False) as response:
                if response.status_code in REDIRECTS and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    return _Download(response.status_code)
                digest = hashlib.sha256()
                size = 0
                with target.open("wb") as out:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_DOWNLOAD:
                            return _Download(200, error="oversized download")
                        digest.update(chunk)
                        out.write(chunk)
                return _Download(200, response.headers.get("etag"), size, digest.hexdigest())
        except httpx2.HTTPError as exc:
            return _Download(0, error=f"download failed: {type(exc).__name__}")
    return _Download(0, error="too many redirects")


def update_inputs(
    folder: Path,
    client: httpx2.Client,
    *,
    key: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    only: list[Source] | None = None,
    region: str = "belgium",
) -> UpdateReport:
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "update-state.json"
    state = _load(state_path)
    now = clock()
    report = UpdateReport([], [], [], {}, None)
    for source in only or sources(region):
        entry = state.get(source.name, {})
        last_check = entry.get("checked_at")
        if last_check and now - datetime.fromisoformat(last_check) < source.min_interval:
            report.skipped.append(source.name)
            continue
        headers = {"User-Agent": "tempo-routing-update"}
        if entry.get("etag") and (folder / source.name).exists():
            headers["If-None-Match"] = entry["etag"]
        part = folder / (source.name + ".part")
        result = _download(client, source.url, headers, key if source.gtfs else None, part)
        try:
            if result.status == 304:
                entry["checked_at"] = now.isoformat()
                state[source.name] = entry
                report.unchanged.append(source.name)
                continue
            if result.error or result.status != 200:
                rejected = " (Belgian Mobility key rejected)" if result.status in (401, 403) else ""
                report.failed[source.name] = result.error or f"HTTP {result.status}{rejected}"
                continue
            if result.size == 0:
                report.failed[source.name] = "empty download"
                continue
            if result.sha256 == entry.get("sha256") and (folder / source.name).exists():
                entry.update(checked_at=now.isoformat(), etag=result.etag)
                state[source.name] = entry
                report.unchanged.append(source.name)
                continue
            if source.gtfs:
                try:
                    last = validate_gtfs(
                        part,
                        now.date(),
                        entry.get("bytes"),
                        forbid_frequencies=source.forbid_frequencies,
                    )
                except InvalidGtfsError as exc:
                    report.failed[source.name] = f"rejected: {exc}"
                    continue
                entry["valid_until"] = None if last is None else last.isoformat()
            part.replace(folder / source.name)
            entry.update(
                sha256=result.sha256,
                bytes=result.size,
                etag=result.etag,
                checked_at=now.isoformat(),
                updated_at=now.isoformat(),
                url=source.url,
            )
            state[source.name] = entry
            report.changed.append(source.name)
        finally:
            part.unlink(missing_ok=True)
    if report.changed:
        report.version = now.strftime("%Y%m%dT%H%M%SZ")
        _atomic_write(folder / "READY", report.version.encode())
    _atomic_write(state_path, json.dumps(state, indent=2).encode())
    return report


def main(argv: list[str]) -> int:
    import os

    logging.basicConfig(level=logging.INFO)
    if len(argv) not in (1, 2) or (len(argv) == 2 and argv[1] not in REGIONS):
        print(
            "usage: python -m tempo.transport.gtfs_update <input-folder> [belgium|brussels]",
            file=sys.stderr,
        )
        return 2
    key_file = Path("/run/secrets") / KEY_SECRET
    key = key_file.read_text(encoding="utf-8").strip() if key_file.exists() else None
    key = key or os.environ.get("TEMPO_BMC_PARTNER_KEY")
    if not key:
        print(str(MissingKeyError()), file=sys.stderr)
        return 2
    with httpx2.Client(timeout=600.0) as client:
        report = update_inputs(
            Path(argv[0]), client, key=key, region=argv[1] if len(argv) == 2 else "belgium"
        )
    print(json.dumps(asdict(report), indent=2))
    return 1 if report.failed and not (report.changed or report.unchanged) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
