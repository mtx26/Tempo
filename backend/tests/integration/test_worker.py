"""Worker jobs on a REAL PostgreSQL (iCal sync, re-plan scheduling, departure check, job runs)."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx2
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tempo.db.models import (
    Base,
    FixedEventRow,
    JobRunRow,
    NotificationRow,
    PhonePositionRow,
    PlaceAliasRow,
    PlaceRow,
    ScheduleVersion,
)
from tempo.planning.service import get_setting, set_setting
from tempo.worker.jobs import (
    MAX_ICS_BYTES,
    JobError,
    departure_job,
    fetch_ics,
    ics_sync,
    replan_job,
    run_job,
)
from tests.test_engine import FakeRouting

pytestmark = pytest.mark.integration
TZ = ZoneInfo("Europe/Brussels")
NOW = datetime(2026, 12, 7, 6, tzinfo=TZ)


def ics(*events: tuple[str, str, str, str]) -> bytes:
    body = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Tempo tests//EN"]
    for uid, start, end, location in events:
        body += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            "DTSTAMP:20261201T000000Z",
            "SUMMARY:Analyse",
            f"LOCATION:{location}",
            f"DTSTART;TZID=Europe/Brussels:{start}",
            f"DTEND;TZID=Europe/Brussels:{end}",
            "END:VEVENT",
        ]
    body.append("END:VCALENDAR")
    return "\r\n".join(body).encode()


FEED = ics(
    ("a@u", "20261207T100000", "20261207T120000", "Auditoire A.1"),
    ("b@u", "20261208T100000", "20261208T120000", "Auditoire A.1"),
    ("c@u", "20261209T140000", "20261209T160000", "Auditoire A.1"),
)


@pytest.fixture
def sessions(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


def configure(sessions: sessionmaker[Session]) -> None:
    with sessions() as s:
        s.add_all(
            [
                PlaceRow(id="home", name="Kot", latitude=50.668, longitude=4.612),
                PlaceRow(id="campus", name="Campus", latitude=50.85, longitude=4.35),
            ]
        )
        s.flush()
        s.add(PlaceAliasRow(alias="auditoire a.1", place_id="campus"))
        set_setting(s, "home_place_id", "home")
        set_setting(s, "horizon_days", 4)
        set_setting(s, "tracker_entity_id", "device_tracker.phone")
        s.commit()


def test_ics_sync_reconciles_notifies_and_requests_a_replan(
    sessions: sessionmaker[Session],
) -> None:
    counts = ics_sync(sessions, FEED, NOW, TZ)
    assert counts["added"] == 3
    with sessions() as s:
        assert len(list(s.scalars(select(FixedEventRow)))) == 3
        assert get_setting(s, "replan_requested") == NOW.isoformat()
        kinds = [n.kind for n in s.scalars(select(NotificationRow))]
        assert kinds == ["ics_changed"]
    assert sum(ics_sync(sessions, FEED, NOW, TZ).values()) == 0
    moved = ics(
        ("a@u", "20261207T100000", "20261207T120000", "Auditoire A.1"),
        ("b@u", "20261208T130000", "20261208T150000", "Auditoire A.1"),
        ("c@u", "20261209T140000", "20261209T160000", "Auditoire A.1"),
    )
    assert ics_sync(sessions, moved, NOW + timedelta(hours=1), TZ)["changed"] == 1


def test_empty_feed_is_suspect_and_deletes_nothing(sessions: sessionmaker[Session]) -> None:
    ics_sync(sessions, FEED, NOW, TZ)
    ics_sync(sessions, ics(), NOW + timedelta(hours=1), TZ)
    with sessions() as s:
        assert all(e.deleted_at is None for e in s.scalars(select(FixedEventRow)))
        assert any(n.kind == "ics_suspect" for n in s.scalars(select(NotificationRow)))


def test_garbage_feed_is_a_job_error(sessions: sessionmaker[Session]) -> None:
    with pytest.raises(JobError):
        ics_sync(sessions, b"<html>maintenance</html>", NOW, TZ)


def test_fetch_ics_guards() -> None:
    secret_url = "https://univ.example/ical?token=SECRET123"

    def ok(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=FEED)

    client = httpx2.Client(transport=httpx2.MockTransport(ok))
    assert fetch_ics(client, secret_url) == FEED
    with pytest.raises(JobError, match="https"):
        fetch_ics(client, "http://univ.example/ical")

    def big(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"x" * (MAX_ICS_BYTES + 1))

    with pytest.raises(JobError, match="10 MB"):
        fetch_ics(httpx2.Client(transport=httpx2.MockTransport(big)), secret_url)

    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(JobError) as caught:
        fetch_ics(httpx2.Client(transport=httpx2.MockTransport(boom)), secret_url)
    assert "SECRET123" not in str(caught.value)

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403)

    with pytest.raises(JobError, match="403"):
        fetch_ics(httpx2.Client(transport=httpx2.MockTransport(forbidden)), secret_url)


def test_replan_scheduling(sessions: sessionmaker[Session]) -> None:
    # Not configured: the user is told, and the job does not loop every tick.
    assert replan_job(sessions, FakeRouting(), NOW, TZ) == "first plan"
    with sessions() as s:
        assert any(n.kind == "setup" for n in s.scalars(select(NotificationRow)))
    assert replan_job(sessions, FakeRouting(), NOW + timedelta(minutes=1), TZ) is None
    configure(sessions)
    later = NOW + timedelta(minutes=20)
    assert replan_job(sessions, FakeRouting(), later, TZ) == "first plan"
    assert replan_job(sessions, FakeRouting(), later + timedelta(minutes=1), TZ) is None
    with sessions() as s:
        set_setting(s, "replan_requested", (later + timedelta(minutes=2)).isoformat())
        s.commit()
    assert replan_job(sessions, FakeRouting(), later + timedelta(minutes=3), TZ) == "requested"
    night = datetime(2026, 12, 8, 3, 1, tzinfo=TZ)
    assert replan_job(sessions, FakeRouting(), night, TZ) == "nightly"
    with sessions() as s:
        assert len(list(s.scalars(select(ScheduleVersion)))) == 3


def test_departure_check_from_fresh_phone_position(sessions: sessionmaker[Session]) -> None:
    configure(sessions)
    ics_sync(sessions, FEED, NOW, TZ)
    replan_job(sessions, FakeRouting(), NOW, TZ)
    now = datetime(2026, 12, 7, 9, 21, tzinfo=TZ)  # outbound planned 09:20 -> 09:50
    assert departure_job(sessions, FakeRouting(), now) == "unknown_position"
    with sessions() as s:
        s.add(
            PhonePositionRow(
                entity_id="device_tracker.phone",
                latitude=50.668,
                longitude=4.612,
                accuracy_m=15,
                observed_at=now - timedelta(minutes=1),
                received_at=now,
            )
        )
        s.commit()
    status = departure_job(sessions, FakeRouting(), now)
    assert status == "at_risk"  # 30 min trip, arrives 09:51 < 10:00 but inside the 10 min margin
    with sessions() as s:
        state = get_setting(s, "departure_check")
        assert state["status"] == "at_risk" and state["event_title"] == "Campus"
        alerts = [n for n in s.scalars(select(NotificationRow)) if n.kind == "departure"]
        assert len(alerts) == 1 and "Campus" in alerts[0].title
    departure_job(sessions, FakeRouting(), now + timedelta(seconds=30))
    with sessions() as s:
        assert len([n for n in s.scalars(select(NotificationRow)) if n.kind == "departure"]) == 1


def test_no_trip_clears_the_departure_state(sessions: sessionmaker[Session]) -> None:
    configure(sessions)
    assert departure_job(sessions, FakeRouting(), NOW) is None
    with sessions() as s:
        assert get_setting(s, "departure_check") is None


def test_failed_job_is_recorded_separately(sessions: sessionmaker[Session]) -> None:
    def broken() -> None:
        raise JobError("boom")

    assert run_job(sessions, "ics_sync", broken) is False
    assert run_job(sessions, "replan", lambda: None) is True
    with sessions() as s:
        runs = {r.name: r for r in s.scalars(select(JobRunRow))}
    assert runs["ics_sync"].last_status == "error" and "boom" in (runs["ics_sync"].last_error or "")
    assert runs["replan"].last_status == "ok"
