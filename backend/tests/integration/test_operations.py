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
from tempo.transport.places import Coordinates
from tempo.transport.routing import Journey, Leg, RoutingUnavailableError
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
    # Why: the free study time before the deadline is smaller than the work (the "core").
    capacity = explained["deadline_capacity"]["Chimie"]
    assert capacity["needed_minutes"] == 3000
    assert 0 < capacity["free_minutes_before_deadline"] < 3000
    assert capacity["deadline"].startswith("2026-12-08")


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
        origin="place:home",
        destination="place:campus",
        arrive_before="2026-12-08T10:00",
    )
    assert arrive["status"] == "ok"
    assert datetime.fromisoformat(arrive["best"]["arrival"]) <= at(8, 10)
    depart = ok(
        session,
        "search_journey",
        origin="place:home",
        destination="place:campus",
        depart_after="2026-12-08T10:00",
    )
    assert datetime.fromisoformat(depart["best"]["departure"]) >= at(8, 10)
    window = ok(
        session,
        "search_journey",
        origin="place:home",
        destination="place:campus",
        depart_after="2026-12-08T09:50",
        arrive_before="2026-12-08T10:05",
    )
    assert window["status"] == "no_candidate"
    assert (
        ok(
            session,
            "search_journey",
            origin="gps",
            destination="place:campus",
            depart_after="2026-12-08T10:00",
        )["status"]
        == "origin_unknown"
    )
    down = run_operation(
        OpContext(session=session, now=NOW, tz=TZ, actor="t", routing=FakeRouting(fail=True)),
        "search_journey",
        {
            "origin": "place:home",
            "destination": "place:campus",
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


class PagedTimetable:
    """MOTIS-like provider: a departure every 15 min, at most 3 journeys per answer; the
    09:45 departure is the only fast one (20 min instead of 50)."""

    def __init__(self, fail_after: int | None = None) -> None:
        self.calls: list[tuple[datetime, bool]] = []
        self.fail_after = fail_after

    def _journey(self, start: datetime) -> Journey:
        trip = timedelta(minutes=20 if start.astimezone(TZ).strftime("%H:%M") == "09:45" else 50)
        leg = Leg("BUS", "a", "b", start, start + trip, start, start + trip, realtime=False)
        return Journey((leg,), start, start + trip, 0, "fake")

    def plan(
        self, origin: Coordinates, destination: Coordinates, time: datetime, *, arrive_by: bool
    ) -> list[Journey]:
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RoutingUnavailableError("MOTIS down")
        self.calls.append((time, arrive_by))
        base = at(8, 6)
        slots = [self._journey(base + timedelta(minutes=15 * i)) for i in range(60)]
        if arrive_by:
            return [j for j in slots if j.arrival <= time][-3:]
        return [j for j in slots if j.departure >= time][:3]


def test_journey_search_explores_several_pages_of_the_range(session: Session) -> None:
    """B09: the best journey of a range is found even when it is not on the first page."""
    seed(session)
    provider = PagedTimetable()
    found = run_operation(
        OpContext(session=session, now=NOW, tz=TZ, actor="t", routing=provider),
        "search_journey",
        {
            "origin": "place:home",
            "destination": "place:campus",
            "depart_after": "2026-12-08T09:00",
            "arrive_before": "2026-12-08T12:00",
        },
    )["result"]
    assert found["status"] == "ok"
    assert datetime.fromisoformat(found["best"]["departure"]) == at(8, 9, 45)
    assert found["best"]["duration_minutes"] == 20
    assert found["pages"] >= 2
    assert all(datetime.fromisoformat(j["arrival"]) <= at(8, 12) for j in found["alternatives"])


def test_journey_search_only_arrival_explores_earlier_pages(session: Session) -> None:
    seed(session)
    provider = PagedTimetable()
    found = run_operation(
        OpContext(session=session, now=NOW, tz=TZ, actor="t", routing=provider),
        "search_journey",
        {
            "origin": "place:home",
            "destination": "place:campus",
            "arrive_before": "2026-12-08T12:00",
        },
    )["result"]
    assert found["status"] == "ok"
    assert found["best"]["duration_minutes"] == 20
    assert all(arrive_by for _, arrive_by in provider.calls)


def test_journey_search_keeps_explored_pages_when_a_later_page_fails(session: Session) -> None:
    seed(session)
    provider = PagedTimetable(fail_after=1)
    found = run_operation(
        OpContext(session=session, now=NOW, tz=TZ, actor="t", routing=provider),
        "search_journey",
        {
            "origin": "place:home",
            "destination": "place:campus",
            "depart_after": "2026-12-08T09:00",
            "arrive_before": "2026-12-08T12:00",
        },
    )["result"]
    assert found["status"] == "ok"
    assert found["exploration_incomplete"] is True
    assert found["best"]["duration_minutes"] == 50


def test_category_windows_restrict_trainings_through_the_conversation(session: Session) -> None:
    """B07: 'my trainings only in the morning, 7-9' constrains placement; removable."""
    seed(session)
    windows = [{"weekday": d, "start": "07:00", "end": "09:00"} for d in (1, 2)]
    ok(session, "set_category_windows", kind="training", windows=windows)
    rules = ok(session, "get_rules")
    assert rules["category_windows"]["training"] == windows
    ok(session, "plan_now")
    training = session.scalars(select(ActivityRow).where(ActivityRow.kind == "training")).one()
    start = training.planned_start_at.astimezone(TZ)  # type: ignore[union-attr]
    assert (
        start.weekday() in (1, 2)
        and start.hour >= 7
        and start + timedelta(hours=1) <= (start.replace(hour=9, minute=0))
    )
    ok(session, "set_category_windows", kind="training", windows=[])
    assert "training" not in ok(session, "get_rules")["category_windows"]
    ok(session, "plan_now")  # local mode: stability keeps the morning slot (B11)
    session.refresh(training)
    assert training.planned_start_at is not None
    ok(session, "plan_now", mode="global")  # explicit reorganisation: preferred slot again
    session.refresh(training)
    assert training.planned_start_at == at(8, 18)


def test_category_windows_reject_unknown_category_and_bad_hours(session: Session) -> None:
    assert "arguments invalides" in err(session, "set_category_windows", kind="race", windows=[])
    assert "fin avant début" in err(
        session,
        "set_category_windows",
        kind="training",
        windows=[{"weekday": 0, "start": "10:00", "end": "09:00"}],
    )


def test_impossible_strict_deadline_gets_simulated_alternatives(session: Session) -> None:
    """B12: deterministic alternatives are simulated (nothing published, nothing changed)."""
    seed(session)
    task = ok(
        session,
        "create_task",
        title="Chimie",
        estimated_minutes=3000,
        target_date="2026-12-08",
        hard_deadline=True,
    )["task"]
    ok(session, "plan_now")
    published = active_version(session)
    found = ok(session, "propose_alternatives")
    assert found["status"] == "alternatives"
    chimie = next(t for t in found["tasks"] if t["task_id"] == task["id"])
    kinds = {a["kind"]: a for a in chimie["alternatives"]}
    assert kinds["postpone_deadline"]["resolves"] is True
    assert kinds["postpone_deadline"]["new_target_date"] > "2026-12-08"
    soft = kinds["make_deadline_soft"]
    assert soft["strict_miss"] is False
    assert soft["unplanned_minutes"] < chimie["unplanned_minutes"]
    reduce = kinds["reduce_volume"]
    assert 0 < reduce["plannable_minutes"] < 3000
    assert active_version(session) == published
    row = session.get(StudyTaskRow, __import__("uuid").UUID(task["id"]))
    assert row is not None and row.target_date == date(2026, 12, 8)
    assert row.target_kind.value == "hard"


def test_alternatives_when_everything_fits(session: Session) -> None:
    seed(session)
    ok(session, "plan_now")
    assert ok(session, "propose_alternatives")["status"] == "nothing_to_resolve"


def test_trainings_and_races_are_created_through_the_conversation(session: Session) -> None:
    """B06: a race is fixed; a training is flexible in its window with a preferred start."""
    seed(session)
    race = ok(
        session,
        "create_event",
        kind="race",
        title="Cross",
        scheduling="fixed",
        start="2026-12-13T10:00",
        duration_minutes=45,
        place_id="club",
    )["activity"]
    assert race["kind"] == "race" and race["scheduling"] == "fixed"
    training = ok(
        session,
        "create_event",
        kind="training",
        title="Footing",
        scheduling="flexible",
        window_start="2026-12-10T00:00",
        window_end="2026-12-11T23:59",
        preferred_start="2026-12-10T17:00",
        duration_minutes=40,
        notes="40 min EF",
    )["activity"]
    assert training["kind"] == "training"
    row = session.get(ActivityRow, __import__("uuid").UUID(training["id"]))
    assert row is not None and row.preferred_start_at == at(10, 17)
    assert "fixe" in err(
        session,
        "create_event",
        kind="race",
        title="10 km",
        scheduling="flexible",
        window_start="2026-12-12T00:00",
        window_end="2026-12-12T23:00",
        duration_minutes=60,
    )
    assert "fenêtre" in err(
        session,
        "create_event",
        kind="training",
        title="x",
        scheduling="flexible",
        window_start="2026-12-10T00:00",
        window_end="2026-12-10T12:00",
        preferred_start="2026-12-11T17:00",
        duration_minutes=40,
    )


class TransferTimetable(PagedTimetable):
    """Every 15 min: odd slots are direct (0 transfer, 60 min), even slots faster but with
    2 transfers and 25 min of walking (40 min)."""

    def _journey(self, start: datetime) -> Journey:
        index = int((start - at(8, 6)).total_seconds() // 900)
        if index % 2:
            leg = Leg(
                "BUS",
                "a",
                "b",
                start,
                start + timedelta(minutes=60),
                start,
                start + timedelta(minutes=60),
                realtime=False,
            )
            return Journey((leg,), start, leg.end, 0, "fake")
        walk = Leg(
            "WALK",
            "a",
            "s",
            start,
            start + timedelta(minutes=25),
            start,
            start + timedelta(minutes=25),
            realtime=False,
        )
        ride = Leg(
            "TRAM",
            "s",
            "b",
            walk.end,
            walk.end + timedelta(minutes=15),
            walk.end,
            walk.end + timedelta(minutes=15),
            realtime=False,
        )
        return Journey((walk, ride), start, ride.end, 2, "fake")


def test_journey_search_criteria_and_return_trip(session: Session) -> None:
    """B09: criteria (max transfers, max walking) and the way back in one search."""
    seed(session)
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="t", routing=TransferTimetable())
    base = {
        "origin": "place:home",
        "destination": "place:campus",
        "depart_after": "2026-12-08T09:00",
        "arrive_before": "2026-12-08T11:00",
    }
    fastest = run_operation(ctx, "search_journey", base)["result"]
    assert fastest["best"]["transfers"] == 2
    direct = run_operation(ctx, "search_journey", {**base, "max_transfers": 0})["result"]
    assert direct["best"]["transfers"] == 0
    short_walk = run_operation(ctx, "search_journey", {**base, "max_walk_minutes": 10})["result"]
    assert short_walk["best"]["transfers"] == 0
    both = run_operation(
        ctx,
        "search_journey",
        {**base, "return_depart_after": "2026-12-08T17:00", "max_transfers": 0},
    )["result"]
    back = both["return"]
    assert back["status"] == "ok"
    assert datetime.fromisoformat(back["best"]["departure"]) >= at(8, 17)
    assert back["best"]["transfers"] == 0
    impossible = run_operation(
        ctx,
        "search_journey",
        {**base, "max_transfers": 0, "max_walk_minutes": 0, "arrive_before": "2026-12-08T10:00"},
    )["result"]
    assert impossible["status"] == "no_candidate" and "critères" in impossible["detail"]


def test_single_bound_search_stays_near_the_requested_time(session: Session) -> None:
    """Arrive before 14:00: a fast journey arriving at 10:05 is not a useful answer."""
    seed(session)
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="t", routing=PagedTimetable())
    found = run_operation(
        ctx,
        "search_journey",
        {
            "origin": "place:home",
            "destination": "place:campus",
            "arrive_before": "2026-12-08T14:00",
        },
    )["result"]
    assert found["status"] == "ok"
    assert datetime.fromisoformat(found["best"]["arrival"]) >= at(8, 12)
    later = run_operation(
        ctx,
        "search_journey",
        {
            "origin": "place:home",
            "destination": "place:campus",
            "depart_after": "2026-12-08T06:00",
        },
    )["result"]
    assert datetime.fromisoformat(later["best"]["departure"]) <= at(8, 8)


def test_direct_journey_between_two_activities_without_going_home(session: Session) -> None:
    """From the class to the training: bounds come from the activities, no return home."""
    seed(session)
    concert = ok(
        session,
        "create_event",
        title="Répétition",
        scheduling="fixed",
        start="2026-12-08T14:00",
        duration_minutes=60,
        place_id="campus",
    )["activity"]
    match = ok(
        session,
        "create_event",
        title="Match",
        scheduling="fixed",
        start="2026-12-08T17:00",
        duration_minutes=60,
        place_id="club",
    )["activity"]
    found = ok(
        session,
        "search_journey",
        origin=f"activity:{concert['id']}",
        destination=f"activity:{match['id']}",
        objective="latest_departure",
    )
    assert found["status"] == "ok" and (found["from"], found["to"]) == ("Campus", "Club")
    assert datetime.fromisoformat(found["best"]["departure"]) >= at(8, 15)
    assert datetime.fromisoformat(found["best"]["arrival"]) <= at(8, 17)
    remote = ok(
        session,
        "create_event",
        title="Visio",
        scheduling="fixed",
        start="2026-12-08T19:00",
        duration_minutes=30,
        remote=True,
    )["activity"]
    nowhere = ok(
        session,
        "search_journey",
        origin=f"activity:{remote['id']}",
        destination="place:home",
        depart_after="2026-12-08T19:30",
    )
    assert nowhere["status"] == "place_unknown" and "pas de lieu" in nowhere["detail"]
    assert "invalide" in err(
        session,
        "search_journey",
        origin="home",
        destination="place:club",
        depart_after="2026-12-08T10:00",
    )


def test_wide_window_and_more_alternatives(session: Session) -> None:
    seed(session)
    provider = PagedTimetable()
    found = run_operation(
        OpContext(session=session, now=NOW, tz=TZ, actor="t", routing=provider),
        "search_journey",
        {
            "origin": "place:home",
            "destination": "place:campus",
            "depart_after": "2026-12-08T06:00",
            "window_minutes": 600,
            "max_alternatives": 8,
        },
    )["result"]
    assert found["pages"] > 5 and len(found["alternatives"]) == 8
    assert datetime.fromisoformat(found["best"]["departure"]) == at(8, 9, 45)  # the fast one


def test_assistant_reads_where_the_user_is_or_will_be(session: Session) -> None:
    """B08: location through a business operation: activity, journey, planned stay, home."""
    seed(session)
    replan(session, FakeRouting(), NOW, TZ, reason="t")
    in_class = ok(session, "get_location", at="2026-12-08T10:30")
    assert (in_class["source"], in_class["place_id"]) == ("schedule", "campus")
    assert ok(session, "get_location", at="2026-12-08T09:50")["source"] == "travel"
    assert ok(session, "get_location", at="2026-12-08T21:00")["place_id"] == "home"
    ok(
        session,
        "set_planned_location",
        place_id="club",
        start="2026-12-13T00:00",
        end="2026-12-14T00:00",
    )
    stay = ok(session, "get_location", at="2026-12-13T15:00")
    assert (stay["source"], stay["place_id"]) == ("planned_location", "club")
    now = ok(session, "get_location")
    assert now["source"] in ("gps", "schedule", "home") and "gps" in now


def test_unreachable_point_is_explained_not_reported_as_a_busy_range(session: Session) -> None:
    class Nothing:
        def plan(self, *args: object, **kwargs: object) -> list[Journey]:
            return []

    seed(session)
    found = run_operation(
        OpContext(session=session, now=NOW, tz=TZ, actor="t", routing=Nothing()),
        "search_journey",
        {"origin": "place:home", "destination": "place:club", "depart_after": "2026-12-08T10:00"},
    )["result"]
    assert (
        found["status"] == "no_candidate" and "aucun itinéraire entre ces points" in found["detail"]
    )
