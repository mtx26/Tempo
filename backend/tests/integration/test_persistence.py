from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tempo.db.models import (
    AuditEntry,
    Base,
    CalendarSource,
    FixedEventRow,
    StudyTaskRow,
    SyncRun,
)
from tempo.domain.models import TargetKind
from tempo.sync.ics import Window, parse_ics
from tempo.sync.ics_repository import active_events, apply_ics_sync, get_or_create_source

pytestmark = pytest.mark.integration

BRUSSELS = ZoneInfo("Europe/Brussels")
FIXTURE = (Path(__file__).parent.parent / "fixtures" / "university.ics").read_bytes()
WINDOW = (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC))


PARSE_MARGIN = timedelta(days=60)


def sync(session: Session, data: bytes = FIXTURE, window: Window = WINDOW) -> SyncRun:
    source = get_or_create_source(session, "university")
    imported = parse_ics(data, window[0], window[1] + PARSE_MARGIN, default_tz=BRUSSELS)
    return apply_ics_sync(session, source, imported, window, now=datetime(2026, 11, 1, tzinfo=UTC))


def test_migrations_match_orm_metadata(engine: Engine) -> None:
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diffs == []


def test_first_sync_inserts_every_occurrence(session: Session) -> None:
    run = sync(session)
    expected = len(parse_ics(FIXTURE, *WINDOW, default_tz=BRUSSELS).events)
    assert run.added == expected
    assert session.scalar(select(func.count()).select_from(FixedEventRow)) == expected


def test_identical_resync_changes_nothing(session: Session) -> None:
    sync(session)
    second = sync(session)
    assert (second.added, second.removed, second.changed) == (0, 0, 0)
    assert second.status == "success"


def test_removed_occurrence_is_soft_deleted_and_restored(session: Session) -> None:
    sync(session)
    without_labo = FIXTURE.replace(b"SUMMARY:Labo", b"SUMMARY:Labo\r\nSTATUS:CANCELLED")
    run = sync(session, without_labo)
    assert run.removed == 1
    source = get_or_create_source(session, "university")
    assert all("Labo" not in e.summary for e in active_events(session, source))
    labo = session.scalars(select(FixedEventRow).where(FixedEventRow.summary == "Labo")).one()
    assert labo.deleted_at is not None  # history kept

    restored = sync(session)
    assert (restored.added, restored.restored) == (0, 1)
    assert labo.deleted_at is None


def test_moved_occurrence_is_updated_in_place(session: Session) -> None:
    sync(session)
    moved = FIXTURE.replace(b"DTSTART:20261022T130000", b"DTSTART:20261022T140000")
    run = sync(session, moved)
    assert run.changed == 1
    labo = session.scalars(select(FixedEventRow).where(FixedEventRow.summary == "Labo")).one()
    assert labo.start_at.astimezone(BRUSSELS).hour == 14


def test_sync_writes_an_audit_entry(session: Session) -> None:
    run = sync(session)
    entry = session.scalars(select(AuditEntry).where(AuditEntry.entity_id == str(run.id))).one()
    assert entry.action == "ics_sync"
    assert entry.details["added"] == run.added


def test_timestamps_round_trip_exact_instants_across_dst(session: Session) -> None:
    sync(session)
    rows = session.scalars(
        select(FixedEventRow).where(FixedEventRow.uid == "analyse-weekly@univ.test")
    ).all()
    hours = sorted(r.start_at.astimezone(UTC).hour for r in rows if r.start_at.month == 10)
    assert 6 in hours  # 08:00 CEST before the change
    assert all(r.start_at.tzinfo is not None for r in rows)


def test_study_task_defaults_to_soft_target(session: Session) -> None:
    task = StudyTaskRow(subject="Analyse", effort_minutes=600, target_date=date(2027, 1, 15))
    session.add(task)
    session.flush()
    session.refresh(task)
    assert task.target_kind == TargetKind.SOFT
    assert task.completed_minutes == 0
    assert task.created_at <= datetime.now(UTC) + timedelta(seconds=5)


def test_migrations_are_reversible(database_url: str) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from tempo.db.migrations import upgrade_to_head
    from tests.integration.conftest import downgrade_to_base

    name = f"{make_url(database_url).database}_rev"
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(database_url).set(database=name).render_as_string(hide_password=False)
    try:
        upgrade_to_head(url)
        downgrade_to_base(url)
        upgrade_to_head(url)  # fails if downgrade left objects (e.g. enum types) behind
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_event_moved_beyond_window_is_changed_not_removed(session: Session) -> None:
    sync(session)
    moved = FIXTURE.replace(b"DTSTART:20261022T130000", b"DTSTART:20270105T130000").replace(
        b"DTEND:20261022T170000", b"DTEND:20270105T170000"
    )
    run = sync(session, moved)
    assert (run.removed, run.changed) == (0, 1)


def test_invalid_source_event_does_not_delete_stored_occurrence(session: Session) -> None:
    sync(session)
    broken = FIXTURE.replace(b"DTEND:20261022T170000", b"DTEND:20261022T120000")
    run = sync(session, broken)
    assert run.removed == 0
    labo = session.scalars(select(FixedEventRow).where(FixedEventRow.summary == "Labo")).one()
    assert labo.deleted_at is None


def test_empty_feed_is_suspect_and_deletes_nothing(session: Session) -> None:
    sync(session)
    empty = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nEND:VCALENDAR\r\n"
    run = sync(session, empty)
    assert run.status == "suspect"
    assert run.removed == 0
    source = get_or_create_source(session, "university")
    assert len(active_events(session, source)) > 0


def test_audit_lists_changed_occurrences_without_event_text(session: Session) -> None:
    sync(session)
    moved = FIXTURE.replace(b"DTSTART:20261022T130000", b"DTSTART:20261022T140000")
    run = sync(session, moved)
    entry = session.scalars(select(AuditEntry).where(AuditEntry.entity_id == str(run.id))).one()
    (change,) = entry.details["changes"]
    assert change["change"] == "changed"
    assert change["fields"] == ["start_at"]
    assert "Labo" not in str(entry.details)


def test_concurrent_syncs_of_same_source_do_not_fail(engine: Engine) -> None:
    import threading

    from sqlalchemy import delete

    name = "concurrency-test"
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with Session(engine) as s:
                barrier.wait()
                source = get_or_create_source(s, name)
                imported = parse_ics(FIXTURE, *WINDOW, default_tz=BRUSSELS)
                apply_ics_sync(s, source, imported, WINDOW, now=datetime(2026, 11, 1, tzinfo=UTC))
                s.commit()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        with Session(engine) as s:
            source = get_or_create_source(s, name)
            assert len(active_events(s, source)) == len(
                parse_ics(FIXTURE, *WINDOW, default_tz=BRUSSELS).events
            )
    finally:
        with Session(engine) as s:
            source_id = get_or_create_source(s, name).id
            s.execute(delete(AuditEntry).where(AuditEntry.details["source"].astext == name))
            s.execute(delete(SyncRun).where(SyncRun.source_id == source_id))
            s.execute(delete(FixedEventRow).where(FixedEventRow.source_id == source_id))
            s.execute(delete(CalendarSource).where(CalendarSource.id == source_id))
            s.commit()


def test_concurrent_migrations_on_fresh_database_succeed(database_url: str) -> None:
    """Several replicas (separate processes) upgrading at once must not fail or deadlock."""
    import subprocess
    import sys

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = f"{make_url(database_url).database}_mig"
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(database_url).set(database=name).render_as_string(hide_password=False)
    code = (
        "import sys; from tempo.db.migrations import upgrade_to_head; upgrade_to_head(sys.argv[1])"
    )
    try:
        procs = [subprocess.Popen([sys.executable, "-c", code, url]) for _ in range(3)]  # noqa: S603
        codes = [p.wait(timeout=120) for p in procs]
        assert codes == [0, 0, 0]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
