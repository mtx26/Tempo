"""End to end: PostgreSQL + planning service + REAL MOTIS (Brussels test region).

Requires TEMPO_TEST_DATABASE_URL and TEMPO_TEST_MOTIS_URL; SKIPPED otherwise. Invariants only.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tempo.db.ics_repository import get_or_create_source
from tempo.db.models import (
    AvailabilityRow,
    Base,
    FixedEventRow,
    PlaceAliasRow,
    PlaceRow,
    StudyBlockRow,
    StudyTaskRow,
)
from tempo.domain.models import EventStatus
from tempo.planning.service import replan, set_setting
from tempo.transport.motis import MotisClient

MOTIS_URL = os.environ.get("TEMPO_TEST_MOTIS_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not MOTIS_URL, reason="TEMPO_TEST_MOTIS_URL not set: real MOTIS not used"),
]
TZ = ZoneInfo("Europe/Brussels")


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


def test_week_planned_with_real_public_transport(session: Session) -> None:
    assert MOTIS_URL is not None
    now = datetime.now(UTC)
    day = (now.astimezone(TZ) + timedelta(days=1)).date()
    session.add_all(
        [
            PlaceRow(id="home", name="Bruxelles-Central", latitude=50.8455, longitude=4.3571),
            PlaceRow(id="ulb", name="ULB Solbosch", latitude=50.8126, longitude=4.3817),
        ]
    )
    session.flush()
    session.add(PlaceAliasRow(alias="ulb - auditoire janson", place_id="ulb"))
    set_setting(session, "home_place_id", "home")
    set_setting(session, "horizon_days", 2)
    for weekday in range(7):
        session.add(
            AvailabilityRow(weekday=weekday, start_minute=480, end_minute=1200, place_id="home")
        )
    source = get_or_create_source(session, "université")
    start = datetime.combine(day, time(10), tzinfo=TZ)
    session.add(
        FixedEventRow(
            source_id=source.id,
            uid="janson",
            start_at=start,
            end_at=start + timedelta(hours=2),
            summary="Analyse",
            location="ULB - Auditoire Janson",
            status=EventStatus.CONFIRMED,
            first_seen_at=now,
            updated_at=now,
        )
    )
    task = StudyTaskRow(subject="maths", effort_minutes=180, target_date=day + timedelta(days=1))
    session.add(task)
    session.flush()

    version = replan(session, MotisClient(MOTIS_URL, timeout_seconds=60), now, TZ, reason="e2e")
    summary = version.summary
    assert version.state == "active", summary
    assert summary["violations"] == []
    outbound = [t for t in summary["travel"] if t["destination"] == "ulb"]
    back = [t for t in summary["travel"] if t["origin"] == "ulb"]
    assert outbound and outbound[0]["status"] == "routed", summary["issues"]
    assert back and back[0]["status"] == "routed"
    trip = outbound[0]
    assert datetime.fromisoformat(trip["arrival"]) <= start - timedelta(minutes=10)
    assert trip["legs"] and any(leg["mode"] != "WALK" for leg in trip["legs"])
    blocks = list(
        session.scalars(
            select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == version.id)
        )
    )
    busy_start = datetime.fromisoformat(trip["start"])
    busy_end = datetime.fromisoformat(back[0]["end"])
    for block in blocks:
        assert block.end_at <= busy_start or block.start_at >= busy_end
    assert sum((b.end_at - b.start_at).total_seconds() // 60 for b in blocks) == 180
