"""Operation catalogue on a REAL PostgreSQL: the acceptance scenarios of ARCHITECTURE.md §12
that do not need Home Assistant or a real LLM (events, series, tasks, work log, rules,
stability, proposals, reminders)."""

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tempo.assistant.catalog import CATALOG, OpContext, expand_all_series, run_operation
from tempo.db.models import (
    ActivityRow,
    NotificationRow,
    ProposalRow,
    RecurrenceExceptionRow,
    ReminderRow,
    StudyBlockRow,
    StudyTaskRow,
    WorkLogRow,
)
from tempo.planning.service import active_version, propose, replan
from tests.integration.test_planning_service import NOW, TZ, at, seed
from tests.test_engine import FakeRouting

pytestmark = pytest.mark.integration


def ctx(session: Session, now: datetime = NOW) -> OpContext:
    return OpContext(session=session, now=now, tz=TZ, actor="test", routing=FakeRouting())


def ok(session: Session, name: str, now: datetime = NOW, **args: Any) -> dict[str, Any]:
    outcome = run_operation(ctx(session, now), name, args)
    assert outcome["ok"], outcome
    result: dict[str, Any] = outcome["result"]
    return result


def err(session: Session, name: str, **args: Any) -> str:
    outcome = run_operation(ctx(session), name, args)
    assert not outcome["ok"], outcome
    return str(outcome["error"])


def test_catalogue_schemas_are_strict_objects() -> None:
    assert len(CATALOG) >= 30
    for op in CATALOG.values():
        schema = op.model.model_json_schema()
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False, op.name


def test_invalid_arguments_and_unknown_operation_are_rejected(session: Session) -> None:
    assert "inconnue" in run_operation(ctx(session), "drop_tables", {})["error"]
    assert "arguments invalides" in err(session, "create_task", title="x", priority=9)
    assert "arguments invalides" in err(session, "create_task", title="x", sql="DROP")


def test_fixed_appointment_in_a_free_slot_moves_nothing(session: Session) -> None:
    """B11: adding an event in free time keeps every existing study block."""
    seed(session)
    first = replan(session, FakeRouting(), NOW, TZ, reason="initial")
    before = {
        (b.start_at, b.end_at)
        for b in session.scalars(
            select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == first.id)
        )
    }
    ok(
        session,
        "create_event",
        title="Dentiste",
        scheduling="fixed",
        start="2026-12-10T21:00",
        duration_minutes=30,
        remote=True,
    )
    result = ok(session, "plan_now")
    assert result["published"] and result["diff"]["moved_blocks"] == 0
    after = {
        (b.start_at, b.end_at)
        for b in session.scalars(
            select(StudyBlockRow).where(
                StudyBlockRow.schedule_version_id == active_version(session).id  # type: ignore[union-attr,arg-type]
            )
        )
    }
    assert after == before


def test_fixed_appointment_over_study_is_respected_with_few_changes(session: Session) -> None:
    """B10/B11: a conflicting appointment displaces only what it overlaps."""
    seed(session)
    first = replan(session, FakeRouting(), NOW, TZ, reason="initial")
    blocks = list(
        session.scalars(select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == first.id))
    )
    target = blocks[0]
    ok(
        session,
        "create_event",
        title="Rendez-vous banque",
        scheduling="fixed",
        start=target.start_at.isoformat(),
        end=target.end_at.isoformat(),
        remote=True,
    )
    result = ok(session, "plan_now")
    assert result["published"]
    assert result["diff"]["kept_blocks"] >= len(blocks) - 2
    version = active_version(session)
    assert version is not None
    for block in session.scalars(
        select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == version.id)
    ):
        assert block.end_at <= target.start_at or block.start_at >= target.end_at


def test_global_reorganisation_only_when_asked(session: Session) -> None:
    seed(session)
    replan(session, FakeRouting(), NOW, TZ, reason="initial")
    assert ok(session, "plan_now", mode="global")["state"] == "active"
    assert active_version(session).summary["mode"] == "global"  # type: ignore[union-attr]


def test_dry_run_publishes_nothing(session: Session) -> None:
    seed(session)
    first = replan(session, FakeRouting(), NOW, TZ, reason="initial")
    result = ok(session, "plan_now", dry_run=True)
    assert result["published"] is False
    assert active_version(session).id == first.id  # type: ignore[union-attr]


def test_all_day_events_informative_or_busy(session: Session) -> None:
    seed(session)
    ok(
        session,
        "create_event",
        title="Anniversaire",
        scheduling="fixed",
        all_day=True,
        start="2026-12-08",
        busy=False,
    )
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    day8 = [b for b in session.scalars(select(StudyBlockRow)) if b.start_at.astimezone(TZ).day == 8]
    assert day8, "an informative all-day event blocks nothing"
    ok(
        session,
        "create_event",
        title="Stage",
        scheduling="fixed",
        all_day=True,
        start="2026-12-09",
        all_day_last_day="2026-12-10",
        busy=True,
    )
    version = replan(session, FakeRouting(), NOW, TZ, reason="t2")
    for block in session.scalars(
        select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == version.id)
    ):
        assert block.start_at.astimezone(TZ).day not in (9, 10)


def test_flexible_outing_is_placed_in_its_window(session: Session) -> None:
    seed(session)
    created = ok(
        session,
        "create_event",
        title="Cinéma",
        scheduling="flexible",
        duration_minutes=150,
        window_start="2026-12-11T18:00",
        window_end="2026-12-11T23:30",
        place_id="campus",
    )
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    row = session.get(ActivityRow, __import__("uuid").UUID(created["activity"]["id"]))
    assert row is not None and row.planned_start_at is not None
    assert at(11, 18) <= row.planned_start_at <= at(11, 21)


def test_unknown_duration_for_flexible_event_is_asked_not_invented(session: Session) -> None:
    seed(session)
    assert "durée inconnue" in err(
        session,
        "create_event",
        title="Sortie",
        scheduling="flexible",
        window_start="2026-12-11T18:00",
        window_end="2026-12-11T23:00",
    )


def test_series_with_exception_following_and_whole_series(session: Session) -> None:
    """B02: recurring event, one-occurrence change, cancel one, this-and-following."""
    seed(session)
    made = ok(
        session,
        "create_event",
        title="Solfège",
        scheduling="fixed",
        start="2026-12-08T19:00",
        duration_minutes=60,
        remote=True,
        rrule="FREQ=WEEKLY;BYDAY=TU;COUNT=6",
    )
    assert made["occurrences_created"] == 6
    rows = list(
        session.scalars(
            select(ActivityRow)
            .where(ActivityRow.series_id.is_not(None))
            .order_by(ActivityRow.earliest_start_at)
        )
    )
    # DST-safe wall clock: every occurrence at 19:00 local
    assert all(r.earliest_start_at.astimezone(TZ).hour == 19 for r in rows)
    ok(session, "update_event", activity_id=str(rows[1].id), start="2026-12-15T20:00")
    ok(session, "cancel_event", activity_id=str(rows[2].id))
    exceptions = list(session.scalars(select(RecurrenceExceptionRow)))
    assert len(exceptions) == 2
    # re-expansion never resurrects the cancelled one nor resets the moved one
    expand_all_series(session, NOW)
    session.refresh(rows[1])
    assert rows[1].earliest_start_at.astimezone(TZ).hour == 20
    assert session.get(ActivityRow, rows[2].id).status == "cancelled"  # type: ignore[union-attr]
    split = ok(
        session,
        "update_event",
        activity_id=str(rows[3].id),
        scope="following",
        title="Solfège avancé",
    )
    assert split["series_id"] != split["previous_series_id"]
    titles = [
        r.title
        for r in session.scalars(
            select(ActivityRow)
            .where(ActivityRow.status == "active", ActivityRow.series_id.is_not(None))
            .order_by(ActivityRow.earliest_start_at)
        )
    ]
    assert titles[:2] == ["Solfège", "Solfège"] and set(titles[2:]) == {"Solfège avancé"}
    new_first = session.scalars(
        select(ActivityRow).where(
            ActivityRow.title == "Solfège avancé", ActivityRow.status == "active"
        )
    ).first()
    assert new_first is not None
    cancelled = ok(session, "cancel_event", activity_id=str(new_first.id), scope="series")
    assert cancelled["cancelled"] >= 1


def test_invalid_rrule_rejected(session: Session) -> None:
    seed(session)
    assert "récurrence" in err(
        session,
        "create_event",
        title="x",
        scheduling="fixed",
        start="2026-12-08T19:00",
        duration_minutes=30,
        rrule="FREQ=MINUTELY",
    )


def test_lock_and_unlock_activity(session: Session) -> None:
    seed(session)
    made = ok(
        session,
        "create_event",
        title="Kiné",
        scheduling="fixed",
        start="2026-12-09T17:00",
        duration_minutes=45,
        remote=True,
    )
    activity_id = made["activity"]["id"]
    assert ok(session, "set_lock", target_id=activity_id, locked=True)["activity"]["locked"]
    assert not ok(session, "set_lock", target_id=activity_id, locked=False)["activity"]["locked"]


def test_task_without_duration_then_estimated_then_partial_progress(session: Session) -> None:
    """B03: kept unplanned without duration; planned once estimated; partial progress."""
    seed(session)
    task = ok(session, "create_task", title="Lettre de motivation", kind="personal")["task"]
    assert task["plannable"] is False
    version = replan(session, FakeRouting(), NOW, TZ, reason="t")
    assert task["id"] not in {
        str(b.task_id)
        for b in session.scalars(
            select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == version.id)
        )
    }
    ok(session, "update_task", task_id=task["id"], estimated_minutes=120, target_date="2026-12-10")
    version = replan(session, FakeRouting(), NOW, TZ, reason="t2")
    planned = sum(
        (b.end_at - b.start_at).total_seconds() // 60
        for b in session.scalars(
            select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == version.id)
        )
        if str(b.task_id) == task["id"]
    )
    assert planned == 120
    done = ok(session, "log_work", task_id=task["id"], minutes=45, idempotency_key="k1")
    assert done["task"]["remaining_minutes"] == 75
    again = ok(session, "log_work", task_id=task["id"], minutes=45, idempotency_key="k1")
    assert again["duplicate"] and again["task"]["remaining_minutes"] == 75


def test_study_volume_with_correction_never_double_counts(session: Session) -> None:
    """B05: 15 h before Friday, blocks near 90 min; done blocks and corrections."""
    seed(session)
    task = ok(
        session, "create_task", title="Physique", estimated_minutes=900, target_date="2026-12-11"
    )["task"]
    version = replan(session, FakeRouting(), NOW, TZ, reason="t")
    blocks = [
        b
        for b in session.scalars(
            select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == version.id)
        )
        if str(b.task_id) == task["id"]
    ]
    lengths = [(b.end_at - b.start_at).total_seconds() // 60 for b in blocks]
    assert sum(lengths) + version.summary["unplanned_minutes"].get(task["id"], 0) == 900
    assert sum(1 for x in lengths if x == 90) >= len(lengths) - 2
    first = blocks[0]
    ok(session, "log_work", block_id=str(first.id))
    assert "déjà" in err(session, "log_work", block_id=str(first.id))
    row = session.get(StudyTaskRow, __import__("uuid").UUID(task["id"]))
    assert row is not None and row.completed_minutes == lengths[0]
    ok(session, "log_work", task_id=task["id"], minutes=-30, note="j'ai surestimé")
    session.refresh(row)
    assert row.completed_minutes == lengths[0] - 30
    assert len(list(session.scalars(select(WorkLogRow).where(WorkLogRow.task_id == row.id)))) == 2
    assert "correction impossible" in err(session, "log_work", task_id=task["id"], minutes=-10_000)


def test_not_enough_time_reports_remainder_and_strict_violation(session: Session) -> None:
    """B12: what fits is planned, the rest stays as remainder, a strict deadline is a miss."""
    seed(session)
    task = ok(
        session,
        "create_task",
        title="Chimie",
        estimated_minutes=3000,
        target_date="2026-12-08",
        hard_deadline=True,
    )["task"]
    result = ok(session, "plan_now")
    assert result["status"] == "partial"
    assert result["unplanned_minutes"][task["id"]] > 0
    assert task["id"] in result["hard_deadline_misses"]
    explained = ok(session, "explain_plan")["active_plan"]
    assert explained["hard_deadline_misses"] == ["Chimie"]


def test_non_splittable_located_task_is_one_session(session: Session) -> None:
    seed(session)
    task = ok(
        session,
        "create_task",
        title="Courses",
        kind="personal",
        estimated_minutes=60,
        place_id="campus",
        splittable=False,
        target_date="2026-12-10",
    )["task"]
    sessions_ = list(session.scalars(select(ActivityRow).where(ActivityRow.kind == "task")))
    assert len(sessions_) == 1 and sessions_[0].duration_minutes == 60
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    session.refresh(sessions_[0])
    assert sessions_[0].planned_start_at is not None
    ok(session, "report_activity", activity_id=str(sessions_[0].id), outcome="done")
    row = session.get(StudyTaskRow, __import__("uuid").UUID(task["id"]))
    assert row is not None and row.archived and row.completed_minutes == 60


def test_dated_exception_blocks_a_day(session: Session) -> None:
    """B07: a dated exception removes availability that day only."""
    seed(session)
    ok(session, "create_task", title="Histoire", estimated_minutes=600, target_date="2026-12-12")
    ok(
        session,
        "add_availability_exception",
        day="2026-12-08",
        available=False,
        reason="journée famille",
    )
    version = replan(session, FakeRouting(), NOW, TZ, reason="t")
    days = {
        b.start_at.astimezone(TZ).day
        for b in session.scalars(
            select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == version.id)
        )
    }
    assert 8 not in days and days & {9, 10}
    rules = ok(session, "get_rules")
    assert rules["exceptions"][0]["reason"] == "journée famille"


def test_planned_location_does_not_touch_gps(session: Session) -> None:
    seed(session)
    ok(
        session,
        "set_planned_location",
        place_id="club",
        start="2026-12-08T00:00",
        end="2026-12-09T00:00",
    )
    assert ok(session, "get_position")["available"] is False
    assert ok(session, "get_rules")["planned_locations"][0]["place_id"] == "club"


def test_obsolete_proposal_is_not_applied(session: Session) -> None:
    """B13: a proposal whose input changed becomes obsolete instead of being applied."""
    seed(session)
    made = ok(
        session,
        "create_event",
        title="Club",
        scheduling="fixed",
        start="2026-12-09T18:00",
        duration_minutes=60,
        remote=True,
    )
    act = session.get(ActivityRow, __import__("uuid").UUID(made["activity"]["id"]))
    assert act is not None
    act.locked = True
    session.flush()
    propose(session, "unlock_activity", "conflit", {"activity_id": str(act.id)}, "k-obsolete")
    proposal = session.scalars(select(ProposalRow)).one()
    act.earliest_start_at = at(9, 19)  # inputs change after the proposal was computed
    act.latest_end_at = at(9, 20)
    session.flush()
    decided = ok(session, "decide_proposal", proposal_id=str(proposal.id), approve=True)
    assert decided["proposal"]["status"] == "obsolete"
    session.refresh(act)
    assert act.locked  # nothing applied


def test_relative_reminder_follows_its_target_without_duplicates(session: Session) -> None:
    """B14: reminder before an activity; moving it moves the reminder; no duplicate."""
    from sqlalchemy.orm import sessionmaker

    from tempo.worker.jobs import reminders_job

    seed(session)
    made = ok(
        session,
        "create_event",
        title="Médecin",
        scheduling="fixed",
        start="2026-12-08T15:00",
        duration_minutes=30,
        remote=True,
    )
    activity_id = made["activity"]["id"]
    ok(
        session,
        "create_reminder",
        message="Médecin dans 30 min",
        target_id=f"activity:{activity_id}",
        offset_minutes=30,
    )
    ok(session, "create_reminder", message="Prendre la carte", at="2026-12-08T14:00")
    session.flush()

    class Once:
        def __call__(self) -> "_Borrowed":
            return _Borrowed(session)

    sessions = Once()
    assert reminders_job(sessions, at(8, 14, 35), TZ) == 2  # type: ignore[arg-type]
    assert reminders_job(sessions, at(8, 14, 40), TZ) == 1  # type: ignore[arg-type]
    notes = [n for n in session.scalars(select(NotificationRow)) if n.kind == "reminder"]
    assert len(notes) == 2  # the repeat is de-duplicated
    ok(session, "update_event", activity_id=activity_id, start="2026-12-08T17:00")
    assert reminders_job(sessions, at(8, 15, 0), TZ) == 0  # type: ignore[arg-type]
    reminders_job(sessions, at(8, 16, 31), TZ)  # type: ignore[arg-type]
    notes = [n for n in session.scalars(select(NotificationRow)) if n.kind == "reminder"]
    assert len(notes) == 3
    ok(session, "cancel_event", activity_id=activity_id)
    reminders_job(sessions, at(8, 16, 40), TZ)  # type: ignore[arg-type]
    statuses = {r.message: r.status for r in session.scalars(select(ReminderRow))}
    assert statuses == {"Médecin dans 30 min": "cancelled", "Prendre la carte": "done"}
    _ = sessionmaker


class _Borrowed:
    """Context manager handing the test session to a job without closing it; ``commit`` is
    a flush so the test transaction can still be rolled back."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def __enter__(self) -> Session:
        return _NoCommit(self.session)  # type: ignore[return-value]

    def __exit__(self, *_: object) -> None:
        return None


class _NoCommit:
    def __init__(self, session: Session) -> None:
        self._session = session

    def commit(self) -> None:
        self._session.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


def test_journey_search_modes_and_errors(session: Session) -> None:
    """B09 (fake routing; the real MOTIS is covered in test_assistant_live_motis)."""
    seed(session)
    arrive = ok(
        session,
        "search_journey",
        origin_place_id="home",
        destination_place_id="campus",
        arrive_before="2026-12-08T10:00",
    )
    assert arrive["status"] == "ok"
    assert datetime.fromisoformat(arrive["best"]["arrival"]) <= at(8, 10)
    depart = ok(
        session,
        "search_journey",
        origin_place_id="home",
        destination_place_id="campus",
        depart_after="2026-12-08T10:00",
    )
    assert datetime.fromisoformat(depart["best"]["departure"]) >= at(8, 10)
    window = ok(
        session,
        "search_journey",
        origin_place_id="home",
        destination_place_id="campus",
        depart_after="2026-12-08T09:50",
        arrive_before="2026-12-08T10:05",
    )
    assert window["status"] == "no_candidate"
    assert (
        ok(
            session,
            "search_journey",
            use_gps=True,
            destination_place_id="campus",
            depart_after="2026-12-08T10:00",
        )["status"]
        == "origin_unknown"
    )
    down = run_operation(
        OpContext(session=session, now=NOW, tz=TZ, actor="t", routing=FakeRouting(fail=True)),
        "search_journey",
        {
            "origin_place_id": "home",
            "destination_place_id": "campus",
            "depart_after": "2026-12-08T10:00",
        },
    )
    assert down["result"]["status"] == "error"


def test_read_operations_answer(session: Session) -> None:
    seed(session)
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    assert ok(session, "get_context")["timezone"] == "Europe/Brussels"
    schedule = ok(session, "get_schedule", start="2026-12-07T00:00", end="2026-12-14T00:00")
    assert {i["type"] for i in schedule["items"]} >= {"class", "study", "race"}
    assert ok(session, "list_tasks")["tasks"][0]["title"] == "maths"
    assert ok(session, "get_sources_status")["llm_required"] is False
    assert ok(session, "explain_plan")["active_plan"]["status"] == "complete"
    assert ok(session, "get_history")["entries"]
    assert "intervalle" in err(
        session, "get_schedule", start="2026-12-07T00:00", end="2027-06-01T00:00"
    )
    _ = (date, timedelta)
