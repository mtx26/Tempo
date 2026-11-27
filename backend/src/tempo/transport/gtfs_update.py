"""Automatic refresh of the routing inputs: Belgian GTFS + OpenStreetMap.

Runs as a one-shot job (``python -m tempo.transport.gtfs_update /routing-input``, nightly in the
stack) writing into the volume shared with the routing service:
- conditional downloads with the stored ETag (``If-None-Match``): verified 2026-11-23 that the
  Belgian Mobility portal answers ``304`` with an empty body for an unchanged feed, and that
  Geofabrik publishes ETags — unchanged feeds cost no bandwidth;
- every GTFS is validated before use (zip integrity, required files, calendar still covering the
  coming week, size not collapsing vs the previous copy); an invalid download is discarded and
  the previous file stays in place (reported, never used);
- ``READY`` points to a complete, versioned snapshot. The supervisor imports that exact
  set while the previous graph continues serving.
"""

import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx2

from tempo.const import MAX_REDIRECTS, REDIRECTS
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
REQUIRED_GTFS = {
    "agency.txt": {"agency_name", "agency_url", "agency_timezone"},
    "stops.txt": {"stop_id", "stop_name", "stop_lat", "stop_lon"},
    "routes.txt": {"route_id", "route_type"},
    "trips.txt": {"route_id", "trip_id", "service_id"},
    "stop_times.txt": {"trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"},
}
MIN_SIZE_RATIO = 0.5
MAX_DOWNLOAD = 2_000_000_000
COVERAGE = timedelta(days=7)
SNAPSHOT_VERSION = re.compile(r"\d{8}T\d{6}Z")


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
) -> date:
    """Raise ``InvalidGtfsError`` unless the feed is usable; returns its last service date."""
    size = data.stat().st_size if isinstance(data, Path) else len(data)
    if previous_size and size < MIN_SIZE_RATIO * previous_size:
        raise InvalidGtfsError(f"size collapsed ({size} vs {previous_size} bytes)")
    try:
        with zipfile.ZipFile(data if isinstance(data, Path) else io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                    raise InvalidGtfsError(f"unsafe zip member: {name!r}")
            if len(names) != len(archive.infolist()):
                raise InvalidGtfsError("duplicate zip members")
            if sum(member.file_size for member in archive.infolist()) > MAX_DOWNLOAD:
                raise InvalidGtfsError("oversized uncompressed archive")
            missing = sorted(set(REQUIRED_GTFS) - names)
            if missing or not ({"calendar.txt", "calendar_dates.txt"} & names):
                raise InvalidGtfsError(f"missing GTFS files: {missing or ['calendar*.txt']}")
            if forbid_frequencies and "frequencies.txt" in names:
                raise InvalidGtfsError(
                    "frequencies.txt present: the realtime start_time repair is no longer valid "
                    "(disable needs_repair in transport/feeds.py and re-check realtime resolution)"
                )
            for name in ("agency.txt", "routes.txt"):
                rows = _rows(archive, name, REQUIRED_GTFS[name])
                if next(rows, None) is None:
                    raise InvalidGtfsError(f"empty {name}")
                rows.close()
            stops = {
                row["stop_id"] for row in _rows(archive, "stops.txt", REQUIRED_GTFS["stops.txt"])
            }
            last, services = _service_dates(archive, names)
            trips = set()
            for row in _rows(archive, "trips.txt", REQUIRED_GTFS["trips.txt"]):
                if not row.get("trip_id") or row.get("service_id") not in services:
                    raise InvalidGtfsError("invalid trip_id or unknown service_id in trips.txt")
                trips.add(row["trip_id"])
            seen = set()
            for row in _rows(archive, "stop_times.txt", REQUIRED_GTFS["stop_times.txt"]):
                if not row.get("trip_id") or row["trip_id"] not in trips:
                    raise InvalidGtfsError("unknown trip_id in stop_times.txt")
                if not row.get("stop_id") or row["stop_id"] not in stops:
                    raise InvalidGtfsError("unknown stop_id in stop_times.txt")
                seen.add(row["trip_id"])
            if not seen:
                raise InvalidGtfsError("empty stop_times.txt")
            if trips != seen:
                raise InvalidGtfsError("trips without stop times")
            if archive.testzip() is not None:
                raise InvalidGtfsError("corrupt zip member")
    except (zipfile.BadZipFile, UnicodeError, csv.Error, RuntimeError, EOFError) as exc:
        raise InvalidGtfsError(f"unreadable GTFS zip: {type(exc).__name__}") from exc
    if last is None:
        raise InvalidGtfsError("no service date in GTFS calendar")
    if last < today + COVERAGE:
        raise InvalidGtfsError(f"calendar ends on {last}, less than a week ahead")
    return last


def _rows(archive: zipfile.ZipFile, name: str, required: set[str]) -> Generator[dict[str, str]]:
    with io.TextIOWrapper(archive.open(name), encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise InvalidGtfsError(f"missing columns in {name}: {sorted(missing)}")
        yield from reader


def _service_dates(archive: zipfile.ZipFile, names: set[str]) -> tuple[date | None, set[str]]:
    last: date | None = None
    services = set()
    for name, column in (("calendar.txt", "end_date"), ("calendar_dates.txt", "date")):
        if name not in names:
            continue
        required = {
            "service_id",
            column,
            "exception_type" if name == "calendar_dates.txt" else "start_date",
        }
        for row in _rows(archive, name, required):
            if name == "calendar_dates.txt" and row.get("exception_type") == "2":
                continue  # a service removal does not extend coverage
            if not row.get("service_id"):
                raise InvalidGtfsError(f"missing service_id in {name}")
            services.add(row["service_id"])
            value = (row.get(column) or "").strip()
            try:
                if len(value) != 8 or not value.isascii() or not value.isdecimal():
                    raise ValueError(value)
                end = date.fromisoformat(value)
            except ValueError as exc:
                raise InvalidGtfsError(f"invalid date in {name}: {value}") from exc
            last = end if last is None else max(last, end)
    return last, services


@dataclass(slots=True)
class UpdateReport:
    changed: list[str]
    unchanged: list[str]
    skipped: list[str]
    failed: dict[str, str]
    version: str | None


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        tmp = Path(file.name)
        file.write(data)
    try:
        tmp.chmod(0o644)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _valid_osm_header(path: Path) -> bool:
    with path.open("rb") as handle:
        size = int.from_bytes(handle.read(4), byteorder="big")
        header = handle.read(size) if 0 < size < 64 * 1024 else b""
        return b"OSMHeader" in header and bool(handle.read(1))


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
    now = clock()
    report = UpdateReport([], [], [], {}, None)
    required = only if only is not None else sources(region)
    ready = folder / "READY"
    previous = ready.read_text(encoding="ascii").strip() if ready.is_file() else ""
    snapshots = folder / "snapshots"
    snapshots.mkdir(exist_ok=True)
    current = snapshots / previous if SNAPSHOT_VERSION.fullmatch(previous) else folder
    published = current != folder and current.is_dir()
    if not published:
        current = folder  # legacy installations keep serving until a full snapshot exists
    state = _load(current / "update-state.json")
    with tempfile.TemporaryDirectory(dir=snapshots, prefix=".staging-") as temp:
        staged = Path(temp)
        _prepare_snapshot(current, staged, client, key, now, required, state, report, published)
        if report.failed:
            report.changed.clear()
            return report
        if not report.changed and published:
            _atomic_write(current / "update-state.json", json.dumps(state, indent=2).encode())
            return report
        if not required or not all((staged / source.name).is_file() for source in required):
            report.failed["snapshot"] = "missing required inputs"
            report.changed.clear()
            return report
        _atomic_write(staged / "update-state.json", json.dumps(state, indent=2).encode())
        version = now.strftime("%Y%m%dT%H%M%SZ")
        candidate = snapshots / version
        if candidate.exists():
            report.failed["snapshot"] = "a snapshot already uses this version"
            report.changed.clear()
            return report
        staged.chmod(0o755)
        staged.rename(candidate)
        _atomic_write(ready, version.encode("ascii"))
        report.version = version
    # Keep the prior publication and recent snapshots a supervisor may still be importing.
    cutoff = (now - timedelta(days=2)).strftime("%Y%m%dT%H%M%SZ")
    for path in snapshots.iterdir():
        if (
            SNAPSHOT_VERSION.fullmatch(path.name)
            and path.name < cutoff
            and path.name != previous
            and path.is_dir()
        ):
            shutil.rmtree(path)
    return report


def _prepare_snapshot(
    current: Path,
    staged: Path,
    client: httpx2.Client,
    key: str,
    now: datetime,
    required: list[Source],
    state: dict[str, Any],
    report: UpdateReport,
    published: bool,
) -> None:
    for source in required:
        entry = state.get(source.name, {})
        destination = current / source.name
        last_check = entry.get("checked_at")
        if (
            published
            and not source.gtfs
            and destination.is_file()
            and destination.stat().st_size > 0
            and last_check
            and now - datetime.fromisoformat(last_check) < source.min_interval
        ):
            if not _valid_osm_header(destination):
                report.failed[source.name] = "invalid OSM PBF header"
                continue
            os.link(destination, staged / source.name)
            report.skipped.append(source.name)
            continue
        headers = {"User-Agent": "tempo-routing-update"}
        if entry.get("etag") and destination.is_file():
            headers["If-None-Match"] = entry["etag"]
        part = staged / (source.name + ".part")
        result = _download(client, source.url, headers, key if source.gtfs else None, part)
        try:
            if result.error or result.status not in (200, 304):
                rejected = " (Belgian Mobility key rejected)" if result.status in (401, 403) else ""
                report.failed[source.name] = result.error or f"HTTP {result.status}{rejected}"
                continue
            if result.status == 200 and result.size == 0:
                report.failed[source.name] = "empty download"
                continue
            if result.status == 304 and (
                not destination.is_file() or destination.stat().st_size == 0
            ):
                report.failed[source.name] = "304 without a usable local copy"
                continue
            unchanged = result.status == 304 or (
                result.sha256 == entry.get("sha256") and destination.is_file()
            )
            if source.gtfs:
                try:
                    if unchanged and published and entry.get("valid_until"):
                        with destination.open("rb") as handle:
                            if hashlib.file_digest(handle, "sha256").hexdigest() != entry.get(
                                "sha256"
                            ):
                                raise InvalidGtfsError("local archive checksum changed")
                        last = date.fromisoformat(entry["valid_until"])
                        if last < now.date() + COVERAGE:
                            raise InvalidGtfsError(
                                f"calendar ends on {last}, less than a week ahead"
                            )
                    else:
                        last = validate_gtfs(
                            destination if result.status == 304 else part,
                            now.date(),
                            entry.get("bytes"),
                            forbid_frequencies=source.forbid_frequencies,
                        )
                except (ValueError, OSError) as exc:
                    report.failed[source.name] = f"rejected: {exc}"
                    continue
                entry["valid_until"] = last.isoformat()
            elif not _valid_osm_header(destination if unchanged else part):
                report.failed[source.name] = "invalid OSM PBF header"
                continue
            if unchanged:
                entry["checked_at"] = now.isoformat()
                if result.status == 200:
                    entry["etag"] = result.etag
                state[source.name] = entry
                os.link(destination, staged / source.name)
                report.unchanged.append(source.name)
                continue
            part.replace(staged / source.name)
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


def main(argv: list[str]) -> int:
    from tempo.logs import configure_logging

    configure_logging()
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
    return int(bool(report.failed))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
