"""The validated decisions, end to end on a REAL PostgreSQL (operations -> planner -> storage).

Each test names the decision it checks (ARCHITECTURE.md §2, §8, §9)."""

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from tempo.assistant.catalog import OpContext, run_operation
from tempo.db.models import (
    ActivityRow,
    FixedEventRow,
    NotificationRow,
    PlaceRow,
    ProposalRow,
    StudyBlockRow,
)
from tempo.db.store import active_version, get_setting, set_setting
from tempo.domain.models import EventStatus
from tempo.planning.service import mark_missed_blocks, replan
from tempo.sync.ics_repository import get_or_create_source
from tempo.worker.jobs import replan_due
from tests.integration.test_planning_service import NOW, TZ, at, seed
from tests.test_engine import FakeRouting


def ctx(session: Session, now: datetime = NOW) -> OpContext:
    return OpContext(session=session, now=now, tz=TZ, actor="test", routing=FakeRouting())


def ok(session: Session, name: str, now: datetime = NOW, **args: Any) -> dict[str, Any]:
    outcome = run_operation(ctx(session, now), name, args)
    assert outcome["ok"], outcome
    result: dict[str, Any] = outcome["result"]
    return result


def blocks(session: Session, **where: Any) -> list[StudyBlockRow]:
    query = select(StudyBlockRow)
    for key, value in where.items():
        query = query.where(getattr(StudyBlockRow, key) == value)
    return list(session.scalars(query))


def lecture(session: Session, uid: str, start: datetime, end: datetime, title: str) -> None:
    source = get_or_create_source(session, "université")
    session.add(
        FixedEventRow(
            source_id=source.id,
            uid=uid,
            start_at=start,
            end_at=end,
            summary=title,
            location="Auditoire A.1",
            status=EventStatus.CONFIRMED,
            first_seen_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()


def test_completed_task_removes_every_remaining_session_including_locked(session: Session) -> None:
    """C16: completed -> remaining sessions (locked ones too) removed, never 'missed'."""
    task = seed(session)
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    planned = sorted(blocks(session, task_id=task.id, state="planned"), key=lambda b: b.start_at)
    ok(session, "log_work", block_id=str(planned[0].id))  # history to keep
    ok(session, "set_lock", target_id=f"block:{planned[-1].id}", locked=True)
    result = ok(session, "update_task", task_id=str(task.id), completed=True)
    assert result["cancelled_sessions"] == len(planned) - 1
    states = {b.id: b.state for b in blocks(session, task_id=task.id)}
    assert states[planned[0].id] == "done"
    assert all(state in ("done", "cancelled") for state in states.values())
    later = planned[-1].end_at + timedelta(hours=1)
    assert mark_missed_blocks(session, later) == 0
    replan(session, FakeRouting(), later, TZ, reason="t")
    kinds = [n.kind for n in session.scalars(select(NotificationRow))]
    assert "missed_block" not in kinds


def test_same_day_move_is_proposed_and_applied_only_on_approval(session: Session) -> None:
    """C31: today's placements move only with approval; the proposed slot is held."""
    seed(session)
    outing = ok(
        session,
        "create_event",
        title="Courses",
        scheduling="flexible",
        duration_minutes=60,
        window_start="2026-12-07T13:00",
        window_end="2026-12-07T19:00",
        remote=True,
    )["activity"]
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    row = session.get(ActivityRow, uuid.UUID(outing["id"]))
    assert row is not None and row.planned_start_at is not None
    first = row.planned_start_at
    ok(
        session,
        "create_event",
        title="Dentiste",
        scheduling="fixed",
        start=first.isoformat(),
        duration_minutes=60,
        remote=True,
    )
    version = replan(session, FakeRouting(), NOW, TZ, reason="t")
    assert row.planned_start_at == first  # not moved automatically
    assert version.summary["status"] == "pending_approval"
    (proposal,) = session.scalars(select(ProposalRow).where(ProposalRow.kind == "move_activity"))
    target = datetime.fromisoformat(proposal.details["to_start"])
    assert target.date() == first.date() and target != first
    # The held slot stays free for it while the user decides.
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    assert row.planned_start_at == first
    ok(session, "decide_proposal", proposal_id=str(proposal.id), approve=True)
    assert row.planned_start_at == target and row.locked is False
    final = replan(session, FakeRouting(), NOW, TZ, reason="t")
    assert row.planned_start_at == target and final.summary["pending_approval"] == []


def test_declined_same_day_move_is_not_replaced_today(session: Session) -> None:
    seed(session)
    outing = ok(
        session,
        "create_event",
        title="Courses",
        scheduling="flexible",
        duration_minutes=60,
        window_start="2026-12-07T13:00",
        window_end="2026-12-08T19:00",
        remote=True,
    )["activity"]
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    row = session.get(ActivityRow, uuid.UUID(outing["id"]))
    assert row is not None and row.planned_start_at is not None
    if row.planned_start_at.astimezone(TZ).date() != NOW.date():
        row.planned_start_at = at(7, 14)
        session.flush()
    ok(
        session,
        "create_event",
        title="Réunion",
        scheduling="fixed",
        start=row.planned_start_at.isoformat(),
        duration_minutes=60,
        remote=True,
    )
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    (proposal,) = session.scalars(select(ProposalRow).where(ProposalRow.kind == "move_activity"))
    ok(session, "decide_proposal", proposal_id=str(proposal.id), approve=False)
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    assert row.planned_start_at is not None
    assert row.planned_start_at.astimezone(TZ).date() == date(2026, 12, 8)


def test_explicit_request_to_reorganise_today_needs_no_second_approval(session: Session) -> None:
    """C31: a user-requested rearrangement of today is not proposed again."""
    seed(session)
    outing = ok(
        session,
        "create_event",
        title="Courses",
        scheduling="flexible",
        duration_minutes=60,
        window_start="2026-12-07T13:00",
        window_end="2026-12-07T19:00",
        remote=True,
    )["activity"]
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    row = session.get(ActivityRow, uuid.UUID(outing["id"]))
    assert row is not None and row.planned_start_at is not None
    ok(
        session,
        "create_event",
        title="Dentiste",
        scheduling="fixed",
        start=row.planned_start_at.isoformat(),
        duration_minutes=60,
        remote=True,
    )
    result = ok(session, "plan_now", allow_today=True)
    assert result["pending_approval"] == []
    assert (
        list(session.scalars(select(ProposalRow).where(ProposalRow.kind == "move_activity"))) == []
    )


def test_general_unavailability_blocks_everything_category_only_its_own(session: Session) -> None:
    """C3: 'not available' covers every flexible activity; 'no training' only trainings."""
    seed(session)
    ok(session, "add_availability_exception", day="2026-12-10", available=False)
    ok(
        session,
        "add_availability_exception",
        day="2026-12-11",
        available=False,
        applies_to="training",
    )
    created = ok(
        session,
        "create_event",
        title="Gainage",
        kind="training",
        scheduling="flexible",
        duration_minutes=45,
        window_start="2026-12-10T06:00",
        window_end="2026-12-12T22:00",
        remote=True,
    )["activity"]
    version = replan(session, FakeRouting(), NOW, TZ, reason="t")
    days = {
        b.start_at.astimezone(TZ).date() for b in blocks(session, schedule_version_id=version.id)
    }
    assert date(2026, 12, 10) not in days
    row = session.get(ActivityRow, uuid.UUID(created["id"]))
    assert row is not None and row.planned_start_at is not None
    assert row.planned_start_at.astimezone(TZ).date() == date(2026, 12, 12)
    rules = ok(session, "get_rules")
    assert {e["applies_to"] for e in rules["exceptions"]} == {"all", "training"}


def test_temporary_preference_expires_without_touching_the_permanent_one(session: Session) -> None:
    """C29: one mechanism for temporary preferences; the permanent value is unchanged."""
    seed(session)
    ok(
        session,
        "update_settings",
        activity_hours={"start": "18:00", "end": "21:00"},
        period_start="2026-12-07T00:00",
        period_end="2026-12-09T00:00",
    )
    assert get_setting(session, "activity_hours") == {"start": "06:00", "end": "22:00"}
    created = ok(
        session,
        "create_event",
        title="Lecture",
        scheduling="flexible",
        duration_minutes=60,
        window_start="2026-12-08T06:00",
        window_end="2026-12-08T23:00",
        remote=True,
    )["activity"]
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    row = session.get(ActivityRow, uuid.UUID(created["id"]))
    assert row is not None and row.planned_start_at is not None
    assert row.planned_start_at.astimezone(TZ).hour >= 18
    assert ok(session, "get_rules")["temporary_preferences"]
    later = ok(session, "get_rules", now=at(10, 8))
    assert later["temporary_preferences"] == []


def test_residence_changes_after_the_last_class_and_asks_without_class(session: Session) -> None:
    """C12: after the last class of the change day; no class that day -> a question."""
    seed(session)
    session.add(PlaceRow(id="dad", name="Papa", latitude=50.60, longitude=4.70))
    session.flush()
    set_setting(
        session,
        "residence_alternation",
        {
            "residences": ["dad", "home"],
            "first_day": "2026-12-11",
            "switch_at": None,
            "period_days": 7,
        },
    )
    set_setting(session, "horizon_days", 9)
    lecture(session, "fri-1", at(11, 8), at(11, 10), "Chimie")
    lecture(session, "fri-2", at(11, 14), at(11, 16), "Physique")
    now = at(10, 20)
    version = replan(session, FakeRouting(), now, TZ, reason="t")
    friday = [
        (t["origin"], t["destination"])
        for t in version.summary["travel"]
        if t["start"].startswith("2026-12-11")
    ]
    assert friday[0] == ("home", "campus") and friday[-1] == ("campus", "dad")
    assert any("18/12" in q for q in version.summary["questions"])
    ok(session, "set_residence_switch", now=now, day="2026-12-18", at="17:00")
    version = replan(session, FakeRouting(), now, TZ, reason="t")
    assert not any("18/12" in q for q in version.summary["questions"])
    open_questions = [
        n
        for n in session.scalars(select(NotificationRow).where(NotificationRow.kind == "question"))
        if n.resolved_at is None and "18/12" in n.message
    ]
    assert open_questions == []  # resolved automatically once answered


def test_exam_gets_the_full_margin_and_the_official_start_is_shown(session: Session) -> None:
    """C4 / C18: no tolerance for exams; the displayed start is always the official one."""
    seed(session)
    lecture(session, "exam", at(10, 9), at(10, 12), "Examen d'analyse")
    version = replan(session, FakeRouting(), NOW, TZ, reason="t")
    trips = [t for t in version.summary["travel"] if t["event_title"] == "Examen d'analyse"]
    assert trips and datetime.fromisoformat(trips[0]["arrival"]) == at(10, 8, 50)
    classes = [t for t in version.summary["travel"] if t["event_title"] == "Analyse"]
    assert classes and datetime.fromisoformat(classes[0]["event_start"]).minute == 0
    assert datetime.fromisoformat(classes[0]["end"]).minute == 10  # attended from 10:10


def test_unconfirmed_journeys_are_refreshed_automatically(session: Session) -> None:
    """C6: during an outage the plan keeps previous itineraries, then refreshes them."""
    seed(session)
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    down = replan(session, FakeRouting(fail=True), NOW + timedelta(minutes=1), TZ, reason="t")
    assert down.state == "active" and down.summary["status"] == "partial"
    assert {t["status"] for t in down.summary["travel"]} == {"stale"}
    assert down.summary["journeys_to_refresh"] > 0
    set_setting(session, "last_replan_attempt", (NOW + timedelta(minutes=1)).isoformat())
    assert replan_due(session, NOW + timedelta(minutes=5), TZ) is None
    assert replan_due(session, NOW + timedelta(minutes=20), TZ) == "journeys to confirm"
    back = replan(session, FakeRouting(), NOW + timedelta(minutes=20), TZ, reason="t")
    assert {t["status"] for t in back.summary["travel"]} == {"routed"}
    assert active_version(session) == back


def test_located_splittable_task_gets_several_sessions(session: Session) -> None:
    """C7: splitting follows the task, not its place; each session has its journeys."""
    seed(session)
    task = ok(
        session,
        "create_task",
        title="Révisions bibliothèque",
        estimated_minutes=240,
        splittable=True,
        place_id="campus",
        target_date="2026-12-11",
    )["task"]
    sessions = list(
        session.scalars(select(ActivityRow).where(ActivityRow.task_id == uuid.UUID(task["id"])))
    )
    assert sorted(s.duration_minutes or 0 for s in sessions) == [60, 90, 90]
    version = replan(session, FakeRouting(), NOW, TZ, reason="t")
    assert version.summary["status"] == "complete", version.summary["issues"]
    for row in sessions:
        assert row.planned_start_at is not None
    # Splitting is always a decision (nature, intent), never a duration threshold.
    for minutes in (45, 180):
        refused = run_operation(
            ctx(session), "create_task", {"title": "Rapport", "estimated_minutes": minutes}
        )
        assert "splittable" in refused["error"]


def test_migration_drops_arbitrary_switch_time_and_removed_preferences(database_url: str) -> None:
    from alembic import command

    from tempo.db.migrations import alembic_config, upgrade_to_head

    name = f"{make_url(database_url).database}_mig"
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(database_url).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "0007")
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO app_setting (key, value) VALUES "
                    "('residence_alternation', CAST(:alt AS jsonb)), "
                    "('study_preferences', CAST(:prefs AS jsonb))"
                ),
                {
                    "alt": '{"value": {"residences": ["a"], "first_day": "2026-11-27", '
                    '"switch_at": "12:00", "period_days": 7}}',
                    "prefs": '{"value": {"preferred_block_minutes": 60, '
                    '"max_daily_study_minutes": 360, "max_blocks_per_subject_per_day": 2, '
                    '"min_gap_same_subject_minutes": 30}}',
                },
            )
        upgrade_to_head(url)
        with engine.connect() as conn:
            rows: dict[str, Any] = dict(
                conn.execute(text("SELECT key, value->'value' FROM app_setting")).all()
            )
        assert rows["residence_alternation"]["switch_at"] is None
        assert rows["study_preferences"] == {"preferred_block_minutes": 60}
        engine.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_uncertain_exam_is_asked_and_planned_on_the_safe_side(session: Session) -> None:
    """An ambiguous title keeps the full margin and asks; the answer applies to the series."""
    seed(session)
    lecture(session, "quiz", at(10, 9), at(10, 11), "Test de chimie")
    version = replan(session, FakeRouting(), NOW, TZ, reason="t")
    assert any("Test de chimie" in q for q in version.summary["questions"])
    trip = next(t for t in version.summary["travel"] if t["event_title"] == "Test de chimie")
    assert datetime.fromisoformat(trip["arrival"]) == at(10, 8, 50)  # no tolerance meanwhile
    question = next(
        n for n in session.scalars(select(NotificationRow)) if "Test de chimie" in n.message
    )
    assert question.due_at == at(10, 9)
    event = session.scalars(select(FixedEventRow).where(FixedEventRow.uid == "quiz")).one()
    ok(session, "set_exam", event_id=f"event:{event.id}", exam=False)
    version = replan(session, FakeRouting(), NOW, TZ, reason="t")
    assert not any("Test de chimie" in q for q in version.summary["questions"])
    trip = next(t for t in version.summary["travel"] if t["event_title"] == "Test de chimie")
    assert datetime.fromisoformat(trip["end"]) == at(10, 9, 10)  # a class again: tolerance
    session.refresh(question)
    assert question.resolved_at is not None


def test_known_busy_weeks_beyond_the_horizon_are_anticipated(session: Session) -> None:
    """C13/C26: a full timetable after the horizon makes Tempo plan more now, and say so."""
    seed(session)  # horizon 6 days from 2026-12-07; study windows 08:00-20:00 every day
    target = date(2026, 12, 27)
    ok(
        session,
        "create_task",
        title="Mémoire",
        estimated_minutes=1800,
        splittable=True,
        target_date=target.isoformat(),
    )
    calm = replan(session, FakeRouting(), NOW, TZ, reason="t").summary["spread"]
    for day in range(14, 28):
        lecture(session, f"busy-{day}", at(day, 8), at(day, 20), "Stage")
    busy = replan(session, FakeRouting(), NOW, TZ, reason="t")
    (share_calm,) = calm.values()
    (share_busy,) = busy.summary["spread"].values()
    # Nothing can be done in the busy weeks: what does not fit there is planned now.
    assert share_busy > share_calm
    assert any("Mémoire" in w for w in busy.summary["warnings"]) or share_busy == 1800


def test_migrations_0008_0009_round_trip_keeps_user_data(database_url: str) -> None:
    from alembic import command

    from tempo.db.migrations import alembic_config, upgrade_to_head

    name = f"{make_url(database_url).database}_rt"
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(database_url).set(database=name).render_as_string(hide_password=False)
    try:
        upgrade_to_head(url)
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO place (id, name, latitude, longitude) "
                    "VALUES ('home', 'Kot', 50, 4);"
                    "INSERT INTO availability_window (id, weekday, start_minute, end_minute) "
                    "VALUES (gen_random_uuid(), 0, 480, 1200);"
                    "INSERT INTO app_setting (key, value) VALUES "
                    '(\'residence_alternation\', \'{"value": {"residences": ["home"], '
                    '"first_day": "2026-11-27", "switch_at": null, "period_days": 7}}\'),'
                    '(\'training_preferences\', \'{"value": {"time_of_day": "evening"}}\')'
                )
            )
        command.downgrade(alembic_config(url), "0007")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT place_id FROM availability_window")).scalar() == "home"
            rows: dict[str, Any] = dict(
                conn.execute(text("SELECT key, value->'value' FROM app_setting")).all()
            )
        assert rows["residence_alternation"]["switch_at"] == "12:00"
        assert rows["training_preferences"] == {"mornings": False}
        upgrade_to_head(url)
        with engine.connect() as conn:
            prefs = conn.execute(
                text("SELECT value->'value' FROM app_setting WHERE key = 'training_preferences'")
            ).scalar()
        assert prefs == {"time_of_day": "none"}
        engine.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
