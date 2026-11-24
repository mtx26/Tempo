"""Automatic routing-input refresh: conditional downloads, validation, atomic switch signal."""

import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from tempo.transport.feeds import KEY_HEADER
from tempo.transport.gtfs_update import (
    InvalidGtfsError,
    Source,
    update_inputs,
    validate_gtfs,
)

NOW = datetime(2026, 11, 24, 2, tzinfo=UTC)


def gtfs(end: str = "20270131", extra: dict[str, str] | None = None, pad: int = 0) -> bytes:
    files = {
        "agency.txt": "agency_id,agency_name\nA,Agency\n",
        "stops.txt": "stop_id,stop_name\nS1,Stop\n",
        "routes.txt": "route_id,agency_id\nR1,A\n",
        "trips.txt": "route_id,service_id,trip_id\nR1,C1,T1\n",
        "stop_times.txt": "trip_id,stop_id,stop_sequence\nT1,S1,1\n" + "x" * pad,
        "calendar.txt": f"service_id,start_date,end_date\nC1,20261101,{end}\n",
        **(extra or {}),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_validation_rules() -> None:
    assert validate_gtfs(gtfs(), NOW.date(), None) == date(2027, 1, 31)
    with pytest.raises(InvalidGtfsError, match="zip"):
        validate_gtfs(b"<html>", NOW.date(), None)
    with pytest.raises(InvalidGtfsError, match="less than a week"):
        validate_gtfs(gtfs(end="20261126"), NOW.date(), None)
    with pytest.raises(InvalidGtfsError, match="collapsed"):
        validate_gtfs(gtfs(), NOW.date(), 10_000_000)
    incomplete = io.BytesIO()
    with zipfile.ZipFile(incomplete, "w") as archive:
        archive.writestr("stops.txt", "stop_id\n")
    with pytest.raises(InvalidGtfsError, match="missing"):
        validate_gtfs(incomplete.getvalue(), NOW.date(), None)


class Portal:
    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {
            "/stib": (gtfs(), '"v1"'),
            "/osm": (b"pbf", '"o1"'),
        }
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        body, etag = self.files[request.url.path]
        if request.headers.get("if-none-match") == etag:
            return httpx2.Response(304)
        return httpx2.Response(200, content=body, headers={"etag": etag})


SOURCES = [
    Source("stib.gtfs.zip", "https://api-management-discovery-production.azure-api.net/stib", True),
    Source("osm.pbf", "https://geofabrik.example/osm", False, timedelta(days=7)),
]


def run(folder: Path, portal: Portal, now: datetime, key: str | None = None) -> object:
    client = httpx2.Client(transport=httpx2.MockTransport(portal))
    return update_inputs(folder, client, key=key, clock=lambda: now, only=SOURCES)


def test_first_run_downloads_validates_and_signals(tmp_path: Path) -> None:
    portal = Portal()
    report = run(tmp_path, portal, NOW, key="secret-key")
    assert report.changed == ["stib.gtfs.zip", "osm.pbf"]  # type: ignore[attr-defined]
    assert (tmp_path / "READY").read_text() == "20261124T020000Z"
    state = json.loads((tmp_path / "update-state.json").read_text())
    assert state["stib.gtfs.zip"]["valid_until"] == "2027-01-31"
    stib, osm = portal.requests
    assert stib.headers[KEY_HEADER] == "secret-key"
    assert KEY_HEADER not in osm.headers  # the portal key never goes to Geofabrik


def test_unchanged_feeds_cost_no_download_and_no_rebuild(tmp_path: Path) -> None:
    portal = Portal()
    run(tmp_path, portal, NOW)
    ready = (tmp_path / "READY").read_text()
    report = run(tmp_path, portal, NOW + timedelta(days=1))
    assert report.unchanged == ["stib.gtfs.zip"] and report.skipped == ["osm.pbf"]  # type: ignore[attr-defined]
    assert portal.requests[-1].headers["if-none-match"] == '"v1"'
    assert (tmp_path / "READY").read_text() == ready
    weekly = run(tmp_path, portal, NOW + timedelta(days=8))
    assert "osm.pbf" in weekly.unchanged  # type: ignore[attr-defined]


def test_invalid_update_keeps_the_previous_file(tmp_path: Path) -> None:
    portal = Portal()
    run(tmp_path, portal, NOW)
    good = (tmp_path / "stib.gtfs.zip").read_bytes()
    portal.files["/stib"] = (gtfs(end="20261125"), '"v2"')
    report = run(tmp_path, portal, NOW + timedelta(days=1))
    assert "stib.gtfs.zip" in report.failed  # type: ignore[attr-defined]
    assert (tmp_path / "stib.gtfs.zip").read_bytes() == good
    assert report.version is None  # type: ignore[attr-defined]


def test_new_valid_version_replaces_and_signals(tmp_path: Path) -> None:
    portal = Portal()
    run(tmp_path, portal, NOW)
    portal.files["/stib"] = (gtfs(end="20270228"), '"v3"')
    report = run(tmp_path, portal, NOW + timedelta(days=1))
    assert report.changed == ["stib.gtfs.zip"]  # type: ignore[attr-defined]
    assert (tmp_path / "READY").read_text() == "20261125T020000Z"
    assert not list(tmp_path.glob("*.part"))


def test_server_errors_are_reported(tmp_path: Path) -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503)

    client = httpx2.Client(transport=httpx2.MockTransport(down))
    report = update_inputs(tmp_path, client, clock=lambda: NOW, only=SOURCES)
    assert report.failed == {"stib.gtfs.zip": "HTTP 503", "osm.pbf": "HTTP 503"}
    assert not (tmp_path / "READY").exists()


def test_key_is_never_forwarded_to_a_redirect_target_and_http_is_refused(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.host == "api-management-discovery-production.azure-api.net":
            return httpx2.Response(302, headers={"location": "https://blob.thirdparty.example/f"})
        return httpx2.Response(200, content=gtfs(), headers={"etag": '"b"'})

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    report = update_inputs(tmp_path, client, key="secret-key", clock=lambda: NOW, only=SOURCES[:1])
    assert report.changed == ["stib.gtfs.zip"]
    assert seen[0].headers[KEY_HEADER] == "secret-key"
    assert KEY_HEADER not in seen[1].headers  # third-party host: key stripped

    def downgrade(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(301, headers={"location": "http://plain.example/f"})

    client = httpx2.Client(transport=httpx2.MockTransport(downgrade))
    report = update_inputs(tmp_path / "x", client, key="k", clock=lambda: NOW, only=SOURCES[:1])
    assert report.failed == {"stib.gtfs.zip": "refusing non-https URL"}


def test_repaired_feed_with_frequencies_is_rejected() -> None:
    """The TEC realtime repair (drop start_time) is only valid without frequencies.txt."""
    with_frequencies = gtfs(extra={"frequencies.txt": "trip_id,start_time\nT1,06:00:00\n"})
    assert validate_gtfs(with_frequencies, NOW.date(), None) == date(2027, 1, 31)
    with pytest.raises(InvalidGtfsError, match="frequencies"):
        validate_gtfs(with_frequencies, NOW.date(), None, forbid_frequencies=True)


def test_sources_per_region() -> None:
    from tempo.transport.gtfs_update import sources

    national = {s.name: s for s in sources()}
    assert set(national) == {
        "tec.gtfs.zip",
        "sncb.gtfs.zip",
        "delijn.gtfs.zip",
        "stib.gtfs.zip",
        "osm.pbf",
    }
    assert national["tec.gtfs.zip"].forbid_frequencies
    assert not national["stib.gtfs.zip"].forbid_frequencies
    brussels = {s.name for s in sources("brussels")}
    assert brussels == {"stib.gtfs.zip", "sncb.gtfs.zip", "osm.pbf"}
    with pytest.raises(ValueError, match="region"):
        sources("mars")


def test_entry_point_reports_json_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tempo.transport import gtfs_update

    seen: dict[str, object] = {}

    def fake(folder: Path, client: object, **kwargs: object) -> object:
        seen.update(folder=folder, **kwargs)
        return gtfs_update.UpdateReport([], [], [], {"stib.gtfs.zip": "timeout"}, None)

    monkeypatch.setattr(gtfs_update, "update_inputs", fake)
    assert gtfs_update.main([str(tmp_path), "brussels"]) == 1  # nothing usable: job fails
    assert seen["region"] == "brussels" and seen["folder"] == tmp_path
    assert json.loads(capsys.readouterr().out)["failed"] == {"stib.gtfs.zip": "timeout"}
    assert gtfs_update.main([str(tmp_path), "mars"]) == 2
