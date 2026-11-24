"""Worker jobs on a REAL PostgreSQL (iCal sync, re-plan scheduling, departure check, job runs)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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


def PUBLIC(host: str) -> bool:  # noqa: N802 - constant-like test helper
    return host != "internal.example"


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
    assert fetch_ics(client, secret_url, host_check=PUBLIC) == FEED
    with pytest.raises(JobError, match="https"):
        fetch_ics(client, "http://univ.example/ical", host_check=PUBLIC)

    def big(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"x" * (MAX_ICS_BYTES + 1))

    with pytest.raises(JobError, match="10 MB"):
        fetch_ics(httpx2.Client(transport=httpx2.MockTransport(big)), secret_url, host_check=PUBLIC)

    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(JobError) as caught:
        fetch_ics(
            httpx2.Client(transport=httpx2.MockTransport(boom)), secret_url, host_check=PUBLIC
        )
    assert "SECRET123" not in str(caught.value)

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403)

    with pytest.raises(JobError, match="403"):
        fetch_ics(
            httpx2.Client(transport=httpx2.MockTransport(forbidden)), secret_url, host_check=PUBLIC
        )


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


def test_fetch_ics_redirects_stay_https_and_public() -> None:
    secret = "https://univ.example/ical?token=SECRET123"

    def redirect_to(location: str) -> httpx2.MockTransport:
        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.host == "univ.example":
                return httpx2.Response(302, headers={"location": location})
            return httpx2.Response(200, content=FEED)

        return httpx2.MockTransport(handler)

    ok = httpx2.Client(transport=redirect_to("https://cdn.example/feed.ics"))
    assert fetch_ics(ok, secret, host_check=PUBLIC) == FEED
    downgrade = httpx2.Client(transport=redirect_to("http://cdn.example/feed.ics"))
    with pytest.raises(JobError, match="https"):
        fetch_ics(downgrade, secret, host_check=PUBLIC)
    internal = httpx2.Client(transport=redirect_to("https://internal.example/x"))
    with pytest.raises(JobError, match="public"):
        fetch_ics(internal, secret, host_check=PUBLIC)

    def invalid(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.InvalidURL("Invalid port: 'SECRET123'")

    with pytest.raises(JobError) as caught:
        fetch_ics(httpx2.Client(transport=httpx2.MockTransport(invalid)), secret, host_check=PUBLIC)
    assert "SECRET123" not in str(caught.value)


def test_private_addresses_are_not_public() -> None:
    from tempo.worker.jobs import is_public_host

    assert not is_public_host("127.0.0.1")
    assert not is_public_host("10.0.0.5")
    assert not is_public_host("no-such-host.invalid")


def test_retention_purges_only_old_data(sessions: sessionmaker[Session]) -> None:
    from tempo.db.models import NotificationRow, ProposalRow
    from tempo.worker.jobs import retention_job

    now = datetime(2027, 6, 1, tzinfo=TZ)
    with sessions() as s:
        s.add(
            NotificationRow(
                kind="k",
                title="t",
                message="m",
                dedupe_key="old",
                created_at=now - timedelta(days=200),
                acknowledged_at=now - timedelta(days=100),
            )
        )
        s.add(
            NotificationRow(
                kind="k",
                title="t",
                message="m",
                dedupe_key="unread",
                created_at=now - timedelta(days=200),
            )
        )
        s.add(
            ProposalRow(
                kind="k",
                title="t",
                details={},
                dedupe_key="p",
                status="pending",
                created_at=now - timedelta(days=400),
            )
        )
        s.add(
            ScheduleVersion(
                state="superseded",
                solver_status="x",
                summary={},
                created_at=now - timedelta(days=40),
            )
        )
        s.add(
            ScheduleVersion(
                state="active", solver_status="x", summary={}, created_at=now - timedelta(days=40)
            )
        )
        s.commit()
    counts = retention_job(sessions, now)
    assert counts["notifications"] == 1 and counts["schedule_versions"] == 1
    assert counts["proposals"] == 0  # pending proposals are never purged
    with sessions() as s:
        assert [n.dedupe_key for n in s.scalars(select(NotificationRow))] == ["unread"]
        assert [v.state for v in s.scalars(select(ScheduleVersion))] == ["active"]


def series(start: str, end: str, count: int = 3, uid: str = "serie@u") -> bytes:
    body = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Tempo tests//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        "DTSTAMP:20261201T000000Z",
        "SUMMARY:Physique",
        "LOCATION:Auditoire A.1",
        f"DTSTART;TZID=Europe/Brussels:{start}",
        f"DTEND;TZID=Europe/Brussels:{end}",
        f"RRULE:FREQ=WEEKLY;COUNT={count}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(body).encode()


def test_moved_series_is_reported_as_changed_not_removed_and_added(
    sessions: sessionmaker[Session],
) -> None:
    ics_sync(sessions, series("20261207T100000", "20261207T120000"), NOW, TZ)
    with sessions() as s:
        ids = sorted(str(e.id) for e in s.scalars(select(FixedEventRow)))
    moved = ics_sync(
        sessions, series("20261207T110000", "20261207T130000"), NOW + timedelta(hours=1), TZ
    )
    assert moved == {"added": 0, "removed": 0, "changed": 3, "restored": 0}
    with sessions() as s:
        rows = list(s.scalars(select(FixedEventRow).order_by(FixedEventRow.start_at)))
    assert sorted(str(r.id) for r in rows) == ids  # same rows, history kept
    assert [r.start_at.astimezone(TZ).hour for r in rows] == [11, 11, 11]
    assert all(r.deleted_at is None for r in rows)


def test_single_event_turned_into_a_series_keeps_its_row(sessions: sessionmaker[Session]) -> None:
    single = ics(("serie@u", "20261207T100000", "20261207T120000", "Auditoire A.1"))
    ics_sync(sessions, single, NOW, TZ)
    counts = ics_sync(
        sessions, series("20261207T100000", "20261207T120000"), NOW + timedelta(hours=1), TZ
    )
    assert counts == {"added": 2, "removed": 0, "changed": 1, "restored": 0}


def test_cancelled_single_occurrence_is_still_a_removal(sessions: sessionmaker[Session]) -> None:
    ics_sync(sessions, FEED, NOW, TZ)
    fewer = ics(
        ("a@u", "20261207T100000", "20261207T120000", "Auditoire A.1"),
        ("b@u", "20261208T100000", "20261208T120000", "Auditoire A.1"),
    )
    counts = ics_sync(sessions, fewer, NOW + timedelta(hours=1), TZ)
    assert counts["removed"] == 1 and counts["changed"] == 0


def test_dst_ambiguous_time_is_stored_as_the_first_instant(sessions: sessionmaker[Session]) -> None:
    feed = ics(("fall@u", "20261025T023000", "20261025T033000", "Auditoire A.1"))
    ics_sync(sessions, feed, datetime(2026, 10, 20, tzinfo=TZ), TZ)
    with sessions() as s:
        event = s.scalars(select(FixedEventRow).where(FixedEventRow.uid == "fall@u")).one()
        warnings = [n.message for n in s.scalars(select(NotificationRow))]
    assert event.start_at.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert any("ambiguous" in m for m in warnings)


class StibRouting:
    """Routing that boards STIB line 59 at stop 3168 (real snapshot fixture)."""

    def plan(self, origin, destination, time, *, arrive_by):  # type: ignore[no-untyped-def]
        from tempo.transport.routing import Journey, Leg

        start = (time - timedelta(minutes=30)) if arrive_by else time
        leg = Leg(
            "BUS",
            "Arrêt",
            "Campus",
            start,
            start + timedelta(minutes=30),
            start,
            start + timedelta(minutes=30),
            realtime=False,
            line="59",
            agency="STIB",
            origin_stop_id="stib_3168",
        )
        return [Journey((leg,), start, start + timedelta(minutes=30), 0, "test")]


class FixtureStib:
    def __init__(self) -> None:
        import json
        from pathlib import Path

        from tempo.transport.stib import (
            StibSnapshot,
            parse_travellers_information,
            parse_waiting_times,
        )

        base = Path(__file__).parents[1] / "fixtures" / "stib"
        self._snapshot = StibSnapshot(
            parse_waiting_times(json.loads((base / "waiting_times.json").read_text("utf-8"))),
            parse_travellers_information(
                json.loads((base / "travellers_information.json").read_text("utf-8"))
            ),
        )
        self.calls = 0

    def snapshot(self):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self._snapshot


def test_departure_check_adds_official_stib_realtime_notes(sessions: sessionmaker[Session]) -> None:
    configure(sessions)
    ics_sync(sessions, FEED, NOW, TZ)
    replan_job(sessions, FakeRouting(), NOW, TZ)
    now = datetime(2026, 11, 23, 23, 0, tzinfo=TZ).astimezone(UTC)
    with sessions() as s:
        s.add(
            PhonePositionRow(
                entity_id="device_tracker.phone",
                latitude=50.668,
                longitude=4.612,
                accuracy_m=15,
                observed_at=now,
                received_at=now,
            )
        )
        version = s.scalars(select(ScheduleVersion).where(ScheduleVersion.state == "active")).one()
        trip = next(t for t in version.summary["travel"] if t["destination"] == "campus")
        shift = datetime.fromisoformat(trip["end"]) - (now + timedelta(minutes=50))
        for t in version.summary["travel"]:
            t["start"] = (datetime.fromisoformat(t["start"]) - shift).isoformat()
            t["end"] = (datetime.fromisoformat(t["end"]) - shift).isoformat()
            t["departure"] = None
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(version, "summary")
        s.commit()
    stib = FixtureStib()
    departure_job(sessions, StibRouting(), now, stib)  # type: ignore[arg-type]
    with sessions() as s:
        state = get_setting(s, "departure_check")
    assert stib.calls == 1, state
    assert any("STIB temps réel : ligne 59" in w for w in state["warnings"])


def test_conversation_retention_purges_old_turns_and_idempotency_keys(
    sessions: sessionmaker[Session],
) -> None:
    from tempo.db.models import ConversationTurnRow
    from tempo.worker.jobs import conversation_retention_job

    now = datetime(2026, 12, 10, 12, tzinfo=UTC)
    with sessions() as s:
        for days in (1, 9):
            at = now - timedelta(days=days)
            s.add(ConversationTurnRow(conversation_id="c", role="user", content="x", created_at=at))
            set_setting(s, f"idem:{days}", {"at": at.isoformat(), "response": {"ok": True}})
        s.commit()
    assert conversation_retention_job(sessions, now) == 2
    with sessions() as s:
        assert len(list(s.scalars(select(ConversationTurnRow)))) == 1
        assert get_setting(s, "idem:1") is not None
        assert get_setting(s, "idem:9") is None
