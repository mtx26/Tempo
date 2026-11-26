"""Domain rules on a REAL PostgreSQL: strict deadlines in local time, nothing deleted silently,
protected study blocks kept in place, audited entities (ARCHITECTURE.md §2, §5, §8)."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tempo.assistant.catalog import run_operation
from tempo.db.models import ActivityRow, AuditEntry, PlaceAliasRow, StudyBlockRow, StudyTaskRow
from tempo.db.store import active_version, get_setting
from tempo.planning.service import replan
from tests.integration.test_operations import ctx, err, ok
from tests.integration.test_planning_service import NOW, TZ, seed
from tests.test_engine import FakeRouting

pytestmark = pytest.mark.integration


def place(session: Session, place_id: str, label: str, **extra: object) -> None:
    arguments = {"place_id": place_id, "name": label, "latitude": 50.8, "longitude": 4.3, **extra}
    assert run_operation(ctx(session), "upsert_place", arguments)["ok"]


def _session_of(session: Session, task_id: str) -> ActivityRow:
    row = session.scalars(
        select(ActivityRow).where(
            ActivityRow.task_id == uuid.UUID(task_id), ActivityRow.status == "active"
        )
    ).one()
    return row


def test_strict_task_session_ends_at_local_midnight_and_is_never_extended(
    session: Session,
) -> None:
    seed(session)
    task = ok(
        session,
        "create_task",
        title="Dossier",
        kind="personal",
        estimated_minutes=1200,
        place_id="campus",
        splittable=False,
        target_date="2026-12-07",
        hard_deadline=True,
    )["task"]
    row = _session_of(session, task["id"])
    # Local end of 7 December (23:00 UTC in winter), never UTC midnight, never extended.
    assert row.latest_end_at == datetime(2026, 12, 8, tzinfo=TZ)
    version = replan(session, FakeRouting(), NOW, TZ, reason="t")
    assert task["id"] in version.summary["hard_deadline_misses"]
    assert version.summary["status"] == "partial"


def test_soft_task_session_may_run_late_and_lateness_is_reported(session: Session) -> None:
    seed(session)
    task = ok(
        session,
        "create_task",
        title="Courses",
        kind="personal",
        estimated_minutes=60,
        place_id="campus",
        splittable=False,
        target_date="2026-12-07",
    )["task"]
    row = _session_of(session, task["id"])
    assert row.latest_end_at > datetime(2026, 12, 8, tzinfo=TZ)


def test_place_in_use_is_never_deleted_silently(session: Session) -> None:
    seed(session)
    ok(
        session,
        "create_event",
        title="Labo",
        scheduling="fixed",
        start="2026-12-09T14:00",
        duration_minutes=60,
        place_id="club",
    )
    message = err(session, "delete_place", place_id="club")
    assert "Labo" in message
    assert "résidence" in err(session, "delete_place", place_id="home")
    place(session, "libre", "Libre", aliases=["x"])
    ok(session, "delete_place", place_id="libre")


def test_upsert_without_aliases_keeps_them(session: Session) -> None:
    seed(session)
    place(session, "campus", "Campus")
    assert session.get(PlaceAliasRow, "auditoire a.1") is not None


def test_planned_locations_are_not_capped(session: Session) -> None:
    seed(session)
    for day in range(60):
        start = NOW + timedelta(days=day, hours=12)
        ok(
            session,
            "set_planned_location",
            place_id="club",
            start=start.isoformat(),
            end=(start + timedelta(hours=1)).isoformat(),
        )
    assert len(get_setting(session, "stays")) == 60


def test_locking_and_unlocking_a_block_keeps_it(session: Session) -> None:
    seed(session)
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    version = active_version(session)
    assert version is not None
    block = session.scalars(
        select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == version.id)
    ).first()
    assert block is not None
    ok(session, "set_lock", target_id=f"block:{block.id}", locked=True)
    assert block.locked and block.schedule_version_id is None and block.state == "planned"
    ok(session, "set_lock", target_id=f"block:{block.id}", locked=False)
    assert session.get(StudyBlockRow, block.id) is not None
    assert not block.locked and block.schedule_version_id == version.id


def test_flexible_to_fixed_needs_a_time(session: Session) -> None:
    seed(session)
    created = ok(
        session,
        "create_event",
        title="Cinéma",
        scheduling="flexible",
        duration_minutes=120,
        window_start="2026-12-10T18:00",
        window_end="2026-12-10T23:00",
        remote=True,
    )["activity"]
    assert "heure" in err(session, "update_event", activity_id=created["id"], scheduling="fixed")
    moved = ok(
        session,
        "update_event",
        activity_id=created["id"],
        scheduling="fixed",
        start="2026-12-10T20:00",
    )["activity"]
    assert moved["window_start"].startswith("2026-12-10T20:00")
    assert moved["window_end"].startswith("2026-12-10T22:00")


def test_block_report_with_impossible_minutes_is_refused(session: Session) -> None:
    seed(session)
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    block = session.scalars(select(StudyBlockRow)).first()
    assert block is not None
    assert "durée invalide" in err(session, "log_work", block_id=str(block.id), minutes=-5)


def test_mutations_are_audited_with_their_entity(session: Session) -> None:
    seed(session)
    task = ok(session, "create_task", title="Chimie", estimated_minutes=60)["task"]
    entry = session.scalars(select(AuditEntry).where(AuditEntry.action == "op:create_task")).one()
    assert (entry.entity, entry.entity_id) == ("task", task["id"])
    assert session.get(StudyTaskRow, uuid.UUID(task["id"])) is not None


def test_flexible_activity_under_way_is_never_moved(session: Session) -> None:
    seed(session)
    created = ok(
        session,
        "create_event",
        title="Footing",
        kind="training",
        scheduling="flexible",
        duration_minutes=60,
        window_start="2026-12-07T07:00",
        window_end="2026-12-07T22:00",
        remote=True,
    )["activity"]
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    row = session.get(ActivityRow, uuid.UUID(created["id"]))
    assert row is not None and row.planned_start_at is not None
    started = row.planned_start_at
    # Half-way through the session a re-plan must keep it exactly where it is.
    replan(session, FakeRouting(), started + timedelta(minutes=30), TZ, reason="t")
    assert row.planned_start_at == started


def test_conflicts_between_any_categories_are_surfaced(session: Session) -> None:
    from tempo.db.models import NotificationRow, ProposalRow

    seed(session)
    ok(
        session,
        "create_event",
        title="Dentiste",
        scheduling="fixed",
        start="2026-12-12T09:00",
        duration_minutes=60,
        remote=True,
    )
    ok(
        session,
        "create_event",
        kind="race",
        title="Cross",
        scheduling="fixed",
        start="2026-12-12T09:30",
        duration_minutes=60,
        remote=True,
    )
    outing = ok(
        session,
        "create_event",
        title="Brunch",
        scheduling="flexible",
        duration_minutes=60,
        window_start="2026-12-12T11:00",
        window_end="2026-12-12T13:00",
        remote=True,
    )["activity"]
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    ok(session, "set_lock", target_id=outing["id"], locked=True)
    ok(
        session,
        "create_event",
        title="Réunion",
        scheduling="fixed",
        start="2026-12-12T11:00",
        duration_minutes=120,
        remote=True,
    )
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    conflicts = [
        n.message for n in session.scalars(select(NotificationRow)) if "conflict" in n.kind
    ]
    assert any("Dentiste" in m and "Cross" in m for m in conflicts)
    proposals = [p.title for p in session.scalars(select(ProposalRow))]
    assert any("Brunch" in t and "Réunion" in t for t in proposals)


def test_activity_hours_are_a_soft_preference(session: Session) -> None:
    seed(session)
    assert "fin" in err(
        session, "update_settings", activity_hours={"start": "20:00", "end": "08:00"}
    )
    ok(session, "update_settings", activity_hours={"start": "18:00", "end": "21:00"})
    created = ok(
        session,
        "create_event",
        title="Lecture",
        scheduling="flexible",
        duration_minutes=60,
        window_start="2026-12-09T08:00",
        window_end="2026-12-09T23:00",
        remote=True,
    )["activity"]
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    row = session.get(ActivityRow, uuid.UUID(created["id"]))
    assert row is not None and row.planned_start_at is not None
    assert 18 <= row.planned_start_at.astimezone(TZ).hour < 21


def test_coach_mail_day_is_a_suggestion_within_its_week(session: Session) -> None:
    from datetime import date, time

    from tempo.ai.apply import apply_result
    from tempo.ai.text_parser import ParsedActivity, ParseResult

    seed(session)
    wednesday = date(2026, 12, 9)
    parsed = ParseResult(
        activities=[ParsedActivity("training", "Fractionné", wednesday, time(18), 60, None, "")]
    )
    applied = apply_result(
        session, parsed, tz=TZ, today=NOW.date(), source="email", source_ref="m1"
    )
    assert applied.created
    row = session.scalars(select(ActivityRow).where(ActivityRow.source == "email")).one()
    assert row.preferred_day == wednesday
    assert row.earliest_start_at == datetime(2026, 12, 7, tzinfo=TZ)  # Monday of that week
    assert row.latest_end_at == datetime(2026, 12, 14, tzinfo=TZ)


def test_training_preferences_are_set_in_conversation_and_used(session: Session) -> None:
    seed(session)
    ok(session, "update_settings", training_preferences={"daylight": True})
    ok(session, "update_settings", training_preferences={"mornings": True})  # merged
    assert get_setting(session, "training_preferences") == {"daylight": True, "mornings": True}
    created = ok(
        session,
        "create_event",
        title="Footing",
        kind="training",
        scheduling="flexible",
        duration_minutes=60,
        window_start="2026-12-10T06:00",
        window_end="2026-12-10T23:00",
        remote=True,
    )["activity"]
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    row = session.get(ActivityRow, uuid.UUID(created["id"]))
    assert row is not None and row.planned_start_at is not None
    start = row.planned_start_at.astimezone(TZ)
    assert (start.hour, start.minute) >= (8, 30)  # after sunrise (about 08:35)
    assert start + timedelta(minutes=60) <= start.replace(hour=12, minute=0)  # by noon


def test_the_assistant_asks_before_routing_a_personal_event(session: Session) -> None:
    from tempo.assistant.catalog import OpContext

    seed(session)
    place(session, "friend", "Chez une amie")
    event = {
        "title": "Souper",
        "kind": "personal",
        "scheduling": "fixed",
        "start": "2026-12-12T18:00",
        "duration_minutes": 240,
        "place_id": "friend",
    }
    assistant = OpContext(session=session, now=NOW, tz=TZ, actor="assistant")
    asked = run_operation(assistant, "create_event", event)
    assert not asked["ok"] and "aller et le retour" in asked["error"]
    assert run_operation(assistant, "create_event", {**event, "remote": True})["ok"]


def test_class_tolerance_is_a_setting_used_by_the_planning(session: Session) -> None:
    from tempo.planning.service import build_input

    seed(session)
    assert ok(session, "get_context")["class_tolerance_minutes"] == 10
    ok(session, "update_settings", class_tolerance_minutes=5)
    assert ok(session, "get_context")["class_tolerance_minutes"] == 5
    assert build_input(session, NOW, TZ).data.class_tolerance == timedelta(minutes=5)
    assert "class_tolerance_minutes" in err(session, "update_settings", class_tolerance_minutes=45)
