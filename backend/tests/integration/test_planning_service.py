"""Planning service on a REAL PostgreSQL: inputs, versions, proposals, notifications."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tempo.db.ics_repository import get_or_create_source
from tempo.db.models import (
    ActivityRow,
    AvailabilityRow,
    FixedEventRow,
    NotificationRow,
    PlaceAliasRow,
    PlaceRow,
    ProposalRow,
    StudyBlockRow,
    StudyTaskRow,
)
from tempo.domain.models import EventStatus
from tempo.planning.service import PlanningNotConfiguredError, replan, set_setting
from tests.test_engine import FakeRouting

TZ = ZoneInfo("Europe/Brussels")
NOW = datetime(2026, 12, 7, 6, tzinfo=TZ)  # Monday


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 12, day, hour, minute, tzinfo=TZ)


def seed(session: Session) -> StudyTaskRow:
    session.add_all(
        [
            PlaceRow(id="home", name="Kot", latitude=50.668, longitude=4.612),
            PlaceRow(id="campus", name="Campus", latitude=50.85, longitude=4.35),
            PlaceRow(id="club", name="Club", latitude=50.70, longitude=4.60),
        ]
    )
    session.flush()
    session.add(PlaceAliasRow(alias="auditoire a.1", place_id="campus"))
    set_setting(session, "home_place_id", "home")
    set_setting(session, "horizon_days", 6)
    for weekday in range(7):
        session.add(
            AvailabilityRow(
                weekday=weekday, start_minute=8 * 60, end_minute=20 * 60, place_id="home"
            )
        )
    source = get_or_create_source(session, "université")
    for d in (7, 8, 9):
        session.add(
            FixedEventRow(
                source_id=source.id,
                uid=f"class-{d}",
                start_at=at(d, 10),
                end_at=at(d, 12),
                summary="Analyse",
                location="Auditoire A.1",
                status=EventStatus.CONFIRMED,
                first_seen_at=NOW,
                updated_at=NOW,
            )
        )
    task = StudyTaskRow(subject="maths", effort_minutes=300, target_date=date(2026, 12, 11))
    session.add(task)
    session.add(
        ActivityRow(
            kind="training",
            title="Fractionné",
            notes="6 x 800 m",
            duration_minutes=60,
            earliest_start_at=at(8, 0),
            latest_end_at=at(9, 23),
            preferred_start_at=at(8, 18),
            place_id="club",
        )
    )
    session.add(
        ActivityRow(
            kind="race",
            title="10 km",
            duration_minutes=60,
            earliest_start_at=at(12, 10),
            latest_end_at=at(12, 11),
            place_id="club",
        )
    )
    session.flush()
    return task


def test_missing_home_is_reported_not_guessed(session: Session) -> None:
    with pytest.raises(PlanningNotConfiguredError):
        replan(session, FakeRouting(), NOW, TZ, reason="test")


def test_replan_persists_an_active_version_with_blocks_travel_and_training(
    session: Session,
) -> None:
    task = seed(session)
    version = replan(session, FakeRouting(), NOW, TZ, reason="test")
    assert version.state == "active"
    assert version.summary["status"] == "complete", version.summary["issues"]
    blocks = list(
        session.scalars(
            select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == version.id)
        )
    )
    assert sum((b.end_at - b.start_at).total_seconds() // 60 for b in blocks) == 300
    assert all(b.task_id == task.id for b in blocks)
    travel = version.summary["travel"]
    assert any(t["destination"] == "campus" and t["status"] == "routed" for t in travel)
    training = session.scalars(select(ActivityRow).where(ActivityRow.kind == "training")).one()
    assert training.planned_start_at == at(8, 18)
    race_trips = [
        t for t in travel if t["destination"] == "club" and t["end"].startswith("2026-12-12")
    ]
    assert race_trips


def test_second_replan_supersedes_and_keeps_blocks_stable(session: Session) -> None:
    seed(session)
    first = replan(session, FakeRouting(), NOW, TZ, reason="first")
    starts_1 = {
        b.start_at
        for b in session.scalars(
            select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == first.id)
        )
    }
    second = replan(session, FakeRouting(), NOW + timedelta(minutes=5), TZ, reason="second")
    session.refresh(first)
    assert first.state == "superseded" and second.state == "active"
    starts_2 = {
        b.start_at
        for b in session.scalars(
            select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == second.id)
        )
    }
    assert starts_1 == starts_2


def test_locked_block_conflict_creates_a_proposal_and_stays(session: Session) -> None:
    task = seed(session)
    locked = StudyBlockRow(task_id=task.id, start_at=at(9, 11), end_at=at(9, 12, 30), locked=True)
    session.add(locked)
    session.flush()
    version = replan(session, FakeRouting(), NOW, TZ, reason="test")
    proposals = list(session.scalars(select(ProposalRow)))
    assert len(proposals) == 1 and proposals[0].kind == "unlock_block"
    assert proposals[0].details["block_id"] == str(locked.id)
    session.refresh(locked)
    assert locked.locked and locked.start_at == at(9, 11)
    assert version.state == "active"
    replan(session, FakeRouting(), NOW, TZ, reason="again")
    assert len(list(session.scalars(select(ProposalRow)))) == 1  # de-duplicated


def test_unknown_location_is_notified_once(session: Session) -> None:
    seed(session)
    source = get_or_create_source(session, "université")
    session.add(
        FixedEventRow(
            source_id=source.id,
            uid="tp",
            start_at=at(10, 14),
            end_at=at(10, 16),
            summary="TP",
            location="Salle inconnue",
            status=EventStatus.CONFIRMED,
            first_seen_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()
    replan(session, FakeRouting(), NOW, TZ, reason="a")
    replan(session, FakeRouting(), NOW, TZ, reason="b")
    notes = [n for n in session.scalars(select(NotificationRow)) if "Salle inconnue" in n.message]
    assert len(notes) == 1


def test_cancelled_and_deleted_events_do_not_block(session: Session) -> None:
    seed(session)
    for event in session.scalars(select(FixedEventRow)):
        event.status = EventStatus.CANCELLED
    session.flush()
    version = replan(session, FakeRouting(), NOW, TZ, reason="test")
    assert not any(t["destination"] == "campus" for t in version.summary["travel"])


def test_past_undone_blocks_are_missed_notified_and_replanned(session: Session) -> None:
    task = seed(session)
    first = replan(session, FakeRouting(), NOW, TZ, reason="first")
    blocks = list(
        session.scalars(select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == first.id))
    )
    later = max(b.end_at for b in blocks[:1]) + timedelta(minutes=1)
    second = replan(session, FakeRouting(), later, TZ, reason="later")
    session.refresh(blocks[0])
    assert blocks[0].state == "missed"
    assert second.summary["missed_blocks"] >= 1
    assert any(n.kind == "missed_block" for n in session.scalars(select(NotificationRow)))
    fresh = list(
        session.scalars(select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == second.id))
    )
    remaining = task.effort_minutes - task.completed_minutes
    assert sum((b.end_at - b.start_at).total_seconds() // 60 for b in fresh) == remaining
    assert all(b.start_at >= later for b in fresh)
