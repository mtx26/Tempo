"""B15 endurance: the REAL worker ``tick`` driven over 7 simulated days on a real PostgreSQL.

Every 10 simulated minutes: phone position pushed, due jobs run (iCal every 30 min, series,
re-plan when requested / nightly / stale, reminders, departure check, daily retention). Events
during the week: the university feed moves a class, routing is down for 4 hours, the user
reports work every evening through the operation catalogue. No LLM, no Home Assistant.

Checks: every job ends healthy, reminders fire exactly once per due instant, one active plan
re-planned at least nightly, the moved class is honoured, no plan overlaps a class, bounded
growth and bounded tick time.
"""

import time as clock
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import tempo.worker.__main__ as worker
from tempo.assistant.catalog import OpContext, run_operation
from tempo.db.models import (
    ActivityRow,
    AvailabilityRow,
    Base,
    FixedEventRow,
    JobRunRow,
    NotificationRow,
    PhonePositionRow,
    PlaceAliasRow,
    PlaceRow,
    ScheduleVersion,
    StudyBlockRow,
)
from tempo.db.store import active_version, set_setting
from tempo.transport.places import Coordinates
from tempo.transport.routing import Journey, RoutingUnavailableError
from tempo.worker.jobs import fetch_ics
from tests.integration.test_planning_service import TZ
from tests.test_engine import FakeRouting

pytestmark = pytest.mark.integration
START = datetime(2026, 12, 7, 6, tzinfo=TZ)
DAYS = 7
STEP = timedelta(minutes=10)
FEED_URL = "https://uni.example/feed.ics"


def feed(moved: bool) -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//soak//EN"]
    for offset in range(DAYS + 2):
        day = (START + timedelta(days=offset)).strftime("%Y%m%d")
        start, end = ("140000", "160000") if moved and offset == 4 else ("100000", "120000")
        lines += [
            "BEGIN:VEVENT",
            f"UID:class-{offset}@u",
            f"DTSTART;TZID=Europe/Brussels:{day}T{start}",
            f"DTEND;TZID=Europe/Brussels:{day}T{end}",
            "SUMMARY:Analyse",
            "LOCATION:Auditoire A.1",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines).encode()


class Clock:
    now = START


class FlakyRouting(FakeRouting):
    """Routing outage on day 3 between 08:00 and 12:00 (simulated clock)."""

    failures = 0

    def plan(
        self, origin: Coordinates, destination: Coordinates, time: datetime, *, arrive_by: bool
    ) -> list[Journey]:
        local = Clock.now.astimezone(TZ)
        if local.date() == date(2026, 12, 9) and 8 <= local.hour < 12:
            self.failures += 1
            raise RoutingUnavailableError("simulated MOTIS outage")
        return super().plan(origin, destination, time, arrive_by=arrive_by)


@pytest.fixture
def sessions(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


def op(session: Session, name: str, **arguments: Any) -> dict[str, Any]:
    ctx = OpContext(session=session, now=Clock.now, tz=TZ, actor="soak", routing=None)
    outcome = run_operation(ctx, name, arguments)
    assert outcome["ok"], outcome
    result: dict[str, Any] = outcome["result"]
    return result


def setup(sessions: sessionmaker[Session]) -> str:
    with sessions() as s:
        s.add_all(
            [
                PlaceRow(id="home", name="Kot", latitude=50.668, longitude=4.612),
                PlaceRow(id="campus", name="Campus", latitude=50.85, longitude=4.35),
                PlaceRow(id="club", name="Club", latitude=50.70, longitude=4.60),
            ]
        )
        s.flush()
        s.add(PlaceAliasRow(alias="auditoire a.1", place_id="campus"))
        s.add_all(
            AvailabilityRow(weekday=d, start_minute=8 * 60, end_minute=20 * 60, place_id="home")
            for d in range(7)
        )
        set_setting(s, "home_place_id", "home")
        set_setting(s, "tracker_entity_id", "device_tracker.phone")
        set_setting(s, "horizon_days", 7)
        task = op(s, "create_task", title="maths", estimated_minutes=900, target_date="2026-12-12")[
            "task"
        ]
        op(
            s,
            "create_event",
            kind="training",
            title="Fractionné",
            scheduling="flexible",
            duration_minutes=60,
            window_start="2026-12-08T00:00",
            window_end="2026-12-10T23:00",
            preferred_start="2026-12-08T18:00",
            place_id="club",
        )
        race = op(
            s,
            "create_event",
            kind="race",
            title="10 km",
            scheduling="fixed",
            start="2026-12-12T10:00",
            duration_minutes=60,
            place_id="club",
        )["activity"]
        op(s, "create_reminder", message="Vitamines", at="2026-12-07T08:00", rrule="FREQ=DAILY")
        op(
            s,
            "create_reminder",
            message="Dossard",
            target_id=f"activity:{race['id']}",
            offset_minutes=90,
        )
        s.commit()
        return str(task["id"])


def push_position(sessions: sessionmaker[Session]) -> None:
    with sessions() as s:
        row = s.get(PhonePositionRow, "device_tracker.phone") or PhonePositionRow(
            entity_id="device_tracker.phone"
        )
        row.latitude, row.longitude, row.accuracy_m = 50.668, 4.612, 12.0
        row.observed_at = row.received_at = Clock.now
        s.add(row)
        s.commit()


def test_worker_week_of_autonomous_operation(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = setup(sessions)
    served = {"moved": False}

    def serve(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=feed(served["moved"]))

    # Test feed host is not resolvable: accept it, keep every other fetch_ics guard.
    monkeypatch.setattr(
        worker, "fetch_ics", lambda http, url: fetch_ics(http, url, host_check=lambda _: True)
    )
    settings = worker.WorkerSettings(
        database_url=SecretStr("postgresql://unused"), ics_url=SecretStr(FEED_URL)
    )
    routing = FlakyRouting()
    durations: list[float] = []
    last_ics = datetime.min.replace(tzinfo=UTC)
    with httpx2.Client(transport=httpx2.MockTransport(serve)) as http:
        while Clock.now < START + timedelta(days=DAYS):
            local = Clock.now.astimezone(TZ)
            if local.date() >= date(2026, 12, 9):
                served["moved"] = True  # the university moves Friday's class
            push_position(sessions)
            due_ics = Clock.now - last_ics >= timedelta(minutes=30)
            began = clock.perf_counter()
            worker.tick(
                sessions, http, routing, settings, TZ, Clock.now, due_ics, due_departure=True
            )
            durations.append(clock.perf_counter() - began)
            last_ics = Clock.now if due_ics else last_ics
            if local.hour == 20 and local.minute == 0:
                with sessions() as s:
                    op(s, "log_work", task_id=task_id, minutes=60, idempotency_key=f"{local:%F}")
                    s.commit()
            Clock.now += STEP

    with sessions() as s:
        jobs = {j.name: j for j in s.scalars(select(JobRunRow))}
        assert {"ics_sync", "series", "replan", "reminders", "departure_check", "retention"} <= set(
            jobs
        )
        unhealthy = {n: j.last_error for n, j in jobs.items() if j.last_status != "ok"}
        assert unhealthy == {}

        reminders = [n.message for n in s.scalars(select(NotificationRow)) if n.kind == "reminder"]
        assert sum("Vitamines" in m for m in reminders) == DAYS  # once per day, never twice
        assert sum("Dossard" in m for m in reminders) == 1

        version = active_version(s)
        assert version is not None
        planned_at = datetime.fromisoformat(version.summary["planned_at"])
        assert Clock.now - planned_at <= timedelta(hours=24)  # re-planned at least nightly
        assert (
            s.scalar(
                select(func.count())
                .select_from(ScheduleVersion)
                .where(ScheduleVersion.state == "active")
            )
            == 1
        )

        moved = s.scalars(select(FixedEventRow).where(FixedEventRow.uid == "class-4@u")).one()
        assert moved.start_at.astimezone(TZ).hour == 14
        classes = [(e.start_at, e.end_at) for e in s.scalars(select(FixedEventRow))]
        for block in s.scalars(
            select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == version.id)
        ):
            assert all(block.end_at <= a or block.start_at >= b for a, b in classes)

        training = s.scalars(select(ActivityRow).where(ActivityRow.kind == "training")).one()
        assert training.planned_start_at is not None or training.latest_end_at < Clock.now

        assert (s.scalar(select(func.count()).select_from(NotificationRow)) or 0) < 150
        assert (s.scalar(select(func.count()).select_from(ScheduleVersion)) or 0) < 120

    assert routing.failures > 0  # the outage was really crossed, then recovered from
    durations.sort()
    p95 = durations[int(len(durations) * 0.95)]
    print(f"\nsoak: {len(durations)} ticks, p95 {p95:.2f}s, max {durations[-1]:.2f}s")
    assert durations[-1] < 15  # one tick never blocks the worker for long
