"""Worker jobs on a REAL PostgreSQL (iCal sync, re-plan scheduling, departure check, job runs)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx2
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tempo.assistant.catalog import OpContext, run_operation
from tempo.const import MAX_ICS_BYTES
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
from tempo.db.store import active_version, get_setting, request_replan, set_setting
from tempo.planning import service
from tempo.planning.engine import OutcomeStatus, PlanningInput, PlanningOutcome, run_planning
from tempo.planning.service import replan
from tempo.transport.routing import RoutingProvider
from tempo.worker import jobs
from tempo.worker.jobs import JobError, departure_job, fetch_ics, ics_sync, replan_job, run_job
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
        set_setting(
            s,
            "residence_alternation",
            {
                "residences": ["home"],
                "first_day": "2026-01-02",
                "switch_at": "12:00",
                "period_days": 7,
            },
        )
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
        setup = next(n for n in s.scalars(select(NotificationRow)) if n.kind == "setup")
        assert "conversation" in setup.message and "service" not in setup.message
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


def test_interrupted_replan_keeps_request_and_retry_delay(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(sessions)
    with sessions.begin() as session:
        set_setting(session, "replan_requested", NOW.isoformat())

    def interrupted(*args: object, **kwargs: object) -> ScheduleVersion:
        raise KeyboardInterrupt

    monkeypatch.setattr(jobs, "replan", interrupted)
    with pytest.raises(KeyboardInterrupt):
        replan_job(sessions, FakeRouting(), NOW, TZ)
    with sessions() as session:
        assert get_setting(session, "replan_requested") == NOW.isoformat()
        assert active_version(session) is None
    assert replan_job(sessions, FakeRouting(), NOW + timedelta(minutes=1), TZ) is None
    monkeypatch.setattr(jobs, "replan", replan)
    assert replan_job(sessions, FakeRouting(), NOW + timedelta(minutes=20), TZ) == "requested"
    with sessions() as session:
        assert get_setting(session, "replan_requested") is None
        assert active_version(session) is not None


def test_busy_planner_leaves_the_worker_request_and_attempt_untouched(
    sessions: sessionmaker[Session],
) -> None:
    configure(sessions)
    with sessions.begin() as session:
        request_replan(session, NOW)
    with sessions.begin() as api:
        api.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": service.REPLAN_LOCK})
        assert replan_job(sessions, FakeRouting(), NOW, TZ) is None
        assert get_setting(api, "last_replan_attempt") is None
        assert get_setting(api, "replan_requested") == NOW.isoformat()
    assert replan_job(sessions, FakeRouting(), NOW, TZ) == "requested"


def test_unknown_replan_does_not_acknowledge_request(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with sessions.begin() as session:
        set_setting(session, "replan_requested", NOW.isoformat())

    def unknown(session: Session, *args: object, **kwargs: object) -> ScheduleVersion:
        version = ScheduleVersion(state="rejected", solver_status="UNKNOWN", summary={})
        session.add(version)
        return version

    monkeypatch.setattr(jobs, "replan", unknown)
    assert replan_job(sessions, FakeRouting(), NOW, TZ) == "requested"
    with sessions() as session:
        assert get_setting(session, "replan_requested") == NOW.isoformat()
    assert replan_job(sessions, FakeRouting(), NOW + timedelta(minutes=1), TZ) is None


@pytest.mark.parametrize("from_assistant", [False, True])
@pytest.mark.parametrize("writer_commits_before_publication", [False, True])
def test_replan_preserves_another_transactions_new_revision(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    from_assistant: bool,
    writer_commits_before_publication: bool,
) -> None:
    configure(sessions)
    with sessions.begin() as session:
        request_replan(session, NOW)

    with sessions.begin() as writer:

        def concurrent_request(data: PlanningInput, routing: RoutingProvider) -> PlanningOutcome:
            # The wall time can coincide. Only the revision distinguishes these requests.
            request_replan(writer, NOW)
            if writer_commits_before_publication:
                writer.commit()
            return run_planning(data, routing)

        monkeypatch.setattr(service, "run_planning", concurrent_request)
        if from_assistant:
            with sessions.begin() as session:
                session.execute(text("SET LOCAL statement_timeout = '5s'"))
                ctx = OpContext(
                    session=session, now=NOW, tz=TZ, actor="test", routing=FakeRouting()
                )
                result = run_operation(ctx, "plan_now", {})
                assert result["ok"] and result["result"]["published"]
        else:

            def replan_with_timeout(
                session: Session,
                routing: RoutingProvider,
                now: datetime,
                tz: ZoneInfo,
                *,
                reason: str,
            ) -> ScheduleVersion:
                session.execute(text("SET LOCAL statement_timeout = '5s'"))
                return service.replan(session, routing, now, tz, reason=reason)

            monkeypatch.setattr(jobs, "replan", replan_with_timeout)
            assert replan_job(sessions, FakeRouting(), NOW, TZ) == "requested"

    with sessions() as session:
        assert active_version(session) is not None
        assert get_setting(session, "replan_requested") == NOW.isoformat()
    monkeypatch.setattr(service, "run_planning", run_planning)
    assert replan_job(sessions, FakeRouting(), NOW + timedelta(minutes=20), TZ) == "requested"
    with sessions() as session:
        assert get_setting(session, "replan_requested") is None


@pytest.mark.parametrize("dry_run", [False, True])
def test_assistant_does_not_acknowledge_unpublished_replan(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    configure(sessions)
    with sessions.begin() as session:
        request_replan(session, NOW)

    def inconclusive(*args: object) -> PlanningOutcome:
        return PlanningOutcome(OutcomeStatus.NO_SOLUTION, None)

    if not dry_run:
        monkeypatch.setattr(service, "run_planning", inconclusive)
    with sessions.begin() as session:
        ctx = OpContext(session=session, now=NOW, tz=TZ, actor="test", routing=FakeRouting())
        result = run_operation(ctx, "plan_now", {"dry_run": dry_run})
        assert result["ok"] and not result["result"]["published"]
    with sessions() as session:
        assert get_setting(session, "replan_requested") == NOW.isoformat()
        assert active_version(session) is None


def exact_class_times(sessions: sessionmaker[Session]) -> None:
    """No class tolerance: these checks are about the margin before the exact start."""
    with sessions() as s:
        set_setting(s, "class_tolerance_minutes", 0)
        s.commit()


def test_departure_check_from_fresh_phone_position(sessions: sessionmaker[Session]) -> None:
    configure(sessions)
    exact_class_times(sessions)
    ics_sync(sessions, FEED, NOW, TZ)
    replan_job(sessions, FakeRouting(), NOW, TZ)
    now = datetime(2026, 12, 7, 9, 21, tzinfo=TZ)  # outbound planned 09:20 -> 09:50
    assert departure_job(sessions, FakeRouting(), now, TZ) == "unknown_position"
    with sessions() as s:
        # D15: an unknown position is a warning the user hears about, never silence.
        unknown = [n for n in s.scalars(select(NotificationRow)) if n.kind == "departure"]
        assert len(unknown) == 1 and "Position inconnue" in unknown[0].title
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
    status = departure_job(sessions, FakeRouting(), now, TZ)
    assert status == "at_risk"  # 30 min trip, arrives 09:51 < 10:00 but inside the 10 min margin
    with sessions() as s:
        state = get_setting(s, "departure_check")
        assert state["status"] == "at_risk" and state["event_title"] == "Analyse"
        official = datetime.fromisoformat(state["event_start"])
        assert official == datetime(2026, 12, 7, 10, 0, tzinfo=TZ)  # official start (C18)
        alerts = [
            n
            for n in s.scalars(select(NotificationRow))
            if n.kind == "departure" and "Arrivée juste" in n.title
        ]
        assert len(alerts) == 1 and "Analyse" in alerts[0].title
        assert "Début : 10:00" in alerts[0].message
    departure_job(sessions, FakeRouting(), now + timedelta(seconds=30), TZ)
    with sessions() as s:
        assert len([n for n in s.scalars(select(NotificationRow)) if n.kind == "departure"]) == 2


def test_departure_reminder_follows_the_plan_without_gps(
    sessions: sessionmaker[Session],
) -> None:
    configure(sessions)
    exact_class_times(sessions)
    ics_sync(sessions, FEED, NOW, TZ)
    replan_job(sessions, FakeRouting(), NOW, TZ)
    now = datetime(2026, 12, 7, 9, 0, tzinfo=TZ)  # planned departure 09:20, no phone position
    assert departure_job(sessions, FakeRouting(), now, TZ) == "unknown_position"
    with sessions() as s:
        reminders = [
            n for n in s.scalars(select(NotificationRow)) if n.kind == "departure_reminder"
        ]
        assert len(reminders) == 1
        assert "début 10:00, départ prévu 09:20" in reminders[0].message
        version = active_version(s)
        assert version is not None
        assert any(t["returning"] for t in version.summary["travel"])


def test_no_trip_clears_the_departure_state(sessions: sessionmaker[Session]) -> None:
    configure(sessions)
    assert departure_job(sessions, FakeRouting(), NOW, TZ) is None
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


def test_retention_runs_once_per_local_day(sessions: sessionmaker[Session]) -> None:
    from tempo.worker.jobs import retention_due

    early = datetime(2026, 12, 8, 3, 0, tzinfo=TZ)
    assert not retention_due(sessions, early, TZ)
    assert retention_due(sessions, early.replace(hour=4, minute=7), TZ)
    assert retention_due(sessions, early.replace(hour=9), TZ)
    jobs.retention_job(sessions, early.replace(hour=9))
    assert not retention_due(sessions, early.replace(hour=9), TZ)
    assert retention_due(sessions, early + timedelta(days=1, hours=2), TZ)


def test_retention_failure_rolls_back_and_retries_only_the_failed_purge(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with sessions.begin() as session:
        session.add(ScheduleVersion(state="rejected", solver_status="UNKNOWN", summary={}))
        session.flush()
        row = session.scalars(select(ScheduleVersion)).one()
        row.created_at = NOW - timedelta(days=365)

    deleted = jobs._deleted

    def fail_after_version_delete(session: Session, statement: object) -> int:
        count = deleted(session, statement)
        if count:
            raise JobError("purge interrupted after deletion")
        return count

    monkeypatch.setattr(jobs, "_deleted", fail_after_version_delete)
    assert not run_job(sessions, "retention", lambda: jobs.retention_job(sessions, NOW))
    assert run_job(
        sessions, "conversation_retention", lambda: jobs.conversation_retention_job(sessions, NOW)
    )
    with sessions() as session:
        assert session.scalars(select(ScheduleVersion)).one() is not None
        run = session.get(JobRunRow, "retention")
        assert run is not None and run.last_status == "error"
        assert get_setting(session, "retention_success_at") is None
        assert get_setting(session, "conversation_retention_success_at") == NOW.isoformat()

    later = NOW + timedelta(minutes=1)
    assert jobs.retention_due(sessions, later, TZ)
    assert not jobs.retention_due(sessions, later, TZ, "conversation_retention")
    monkeypatch.setattr(jobs, "_deleted", deleted)
    assert jobs.retention_job(sessions, later)["schedule_versions"] == 1
    assert not jobs.retention_due(sessions, later, TZ)


def test_run_job_survives_failure_to_record_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object) -> None:
        raise JobError("database unavailable")

    monkeypatch.setattr(jobs, "record_run", fail)
    assert not run_job(sessionmaker(), "retention", fail)


def test_skipped_ics_items_are_notified(sessions: sessionmaker[Session]) -> None:
    feed = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:allday@u\r\nDTSTART;VALUE=DATE:20261209\r\n"
        b"SUMMARY:Session d'examens\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    ics_sync(sessions, feed, NOW, TZ)
    with sessions() as s:
        warnings = [
            n.message for n in s.scalars(select(NotificationRow)) if n.kind == "ics_warning"
        ]
    assert any("allday@u" in w for w in warnings)


def test_relative_reminder_ends_after_its_target(sessions: sessionmaker[Session]) -> None:
    from tempo.db.models import ReminderRow
    from tempo.worker.jobs import reminders_job

    configure(sessions)
    with sessions() as s:
        ctx = OpContext(session=s, now=NOW, tz=TZ, actor="test")
        event = run_operation(
            ctx,
            "create_event",
            {
                "title": "Médecin",
                "scheduling": "fixed",
                "start": "2026-12-08T09:00",
                "duration_minutes": 30,
                "remote": True,
            },
        )["result"]["activity"]
        run_operation(
            ctx,
            "create_reminder",
            {
                "message": "Carte vitale",
                "target_id": f"activity:{event['id']}",
                "offset_minutes": 30,
            },
        )
        s.commit()
    assert reminders_job(sessions, datetime(2026, 12, 8, 8, 31, tzinfo=TZ), TZ) == 1
    reminders_job(sessions, datetime(2026, 12, 8, 16, 0, tzinfo=TZ), TZ)
    with sessions() as s:
        assert s.scalars(select(ReminderRow)).one().status == "done"
