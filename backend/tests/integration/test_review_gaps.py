"""Architecture-review gaps on a REAL PostgreSQL: protected-activity conflicts, coach changes as
proposals, configurable departure reminders, conversational approve/reject/done."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tempo.api.app import create_app
from tempo.config import Settings
from tempo.db.ics_repository import get_or_create_source
from tempo.db.models import (
    ActivityRow,
    Base,
    FixedEventRow,
    NotificationRow,
    PhonePositionRow,
    ProposalRow,
    StudyBlockRow,
)
from tempo.domain.models import EventStatus
from tempo.planning.service import replan, set_setting
from tempo.sync.coach_import import apply_weekly_plan
from tempo.sync.coach_plan import parse_plan_docx
from tempo.worker.jobs import departure_job
from tests.docx_fixtures import EXAMPLE_WEEK, plan_docx
from tests.integration.test_planning_service import seed
from tests.test_engine import FakeRouting

pytestmark = pytest.mark.integration
TZ = ZoneInfo("Europe/Brussels")
NOW = datetime(2026, 12, 7, 6, tzinfo=TZ)
TOKEN = "Hq8Wm2Zr5Tx9Lb3Nc7Vd1Kf4Gs6Pj0YeUa"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 12, day, hour, minute, tzinfo=TZ)


@pytest.fixture
def sessions(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


def add_class(s: Session, uid: str, start: datetime, end: datetime) -> None:
    source = get_or_create_source(s, "université")
    s.add(
        FixedEventRow(
            source_id=source.id,
            uid=uid,
            start_at=start,
            end_at=end,
            summary="TP",
            location="Auditoire A.1",
            status=EventStatus.CONFIRMED,
            first_seen_at=NOW,
            updated_at=NOW,
        )
    )


def test_class_over_locked_training_or_race_is_surfaced(sessions: sessionmaker[Session]) -> None:
    with sessions() as s:
        seed(s)
        training = s.scalars(select(ActivityRow).where(ActivityRow.kind == "training")).one()
        training.planned_start_at, training.locked = at(8, 18), True
        add_class(s, "late-tp", at(8, 17, 30), at(8, 19))
        add_class(s, "race-day", at(12, 9), at(12, 12))
        replan(s, FakeRouting(), NOW, TZ, reason="conflicts")
        s.commit()
        proposals = list(s.scalars(select(ProposalRow)))
        kinds = sorted(p.kind for p in proposals)
        assert kinds == ["unlock_activity"]
        assert any(n.kind == "race_conflict" for n in s.scalars(select(NotificationRow)))


def test_coach_change_on_protected_session_becomes_a_proposal(
    sessions: sessionmaker[Session], database_url: str
) -> None:
    today = date(2026, 11, 20)
    with sessions() as s:
        apply_weekly_plan(s, parse_plan_docx(plan_docx(), today), tz=TZ, today=today, origin="m1")
        row = s.scalars(select(ActivityRow).where(ActivityRow.duration_minutes == 75)).one()
        row.notes, row.user_modified = "ma version", True
        s.commit()
        row_id = row.id
    week = {k: list(v) for k, v in EXAMPLE_WEEK.items()}
    week["Séance"][4] = "Endurance fondamentale 1h05'"
    with sessions() as s:
        apply_weekly_plan(
            s, parse_plan_docx(plan_docx(week=week), today), tz=TZ, today=today, origin="m2"
        )
        s.commit()
        proposal = s.scalars(select(ProposalRow)).one()
        assert proposal.kind == "coach_plan_change"
        kept = s.get(ActivityRow, row_id)
        assert kept is not None and kept.notes == "ma version"  # not overwritten
    with TestClient(create_app(Settings(api_token=TOKEN, database_url=database_url))) as api:
        decided = api.post(
            "/v1/operations/decide_proposal",
            json={"proposal_id": str(proposal.id), "approve": True},
            headers=AUTH,
        )
        assert decided.status_code == 200, decided.text
    with sessions() as s:
        updated = s.get(ActivityRow, row_id)
        assert updated is not None and updated.duration_minutes == 65
        assert not updated.user_modified and "ma version" not in updated.notes


def test_departure_reminders_fire_once_per_threshold(sessions: sessionmaker[Session]) -> None:
    with sessions() as s:
        seed(s)
        set_setting(s, "tracker_entity_id", "device_tracker.phone")
        set_setting(s, "reminder_minutes", [60, 15])
        version = replan(s, FakeRouting(), NOW, TZ, reason="r")
        s.commit()
        trip = next(t for t in version.summary["travel"] if t["destination"] == "campus")
    leave = datetime.fromisoformat(trip["start"])

    def check(minutes_before: int) -> None:
        now = (leave - timedelta(minutes=minutes_before)).astimezone(UTC)
        with sessions() as s:
            s.merge(
                PhonePositionRow(
                    entity_id="device_tracker.phone",
                    latitude=50.668,
                    longitude=4.612,
                    accuracy_m=10,
                    observed_at=now,
                    received_at=now,
                )
            )
            s.commit()
        departure_job(sessions, FakeRouting(), now)

    for minutes in (50, 40, 10, 5):
        check(minutes)
    with sessions() as s:
        reminders = [
            n.title for n in s.scalars(select(NotificationRow)) if n.kind == "departure_reminder"
        ]
    assert len(reminders) == 2 and reminders[0].startswith("Départ dans 50 min")


def test_conversational_approve_and_done(
    sessions: sessionmaker[Session], database_url: str
) -> None:
    with sessions() as s:
        seed(s)
        task_id = s.scalars(select(StudyBlockRow.task_id)).first()
        version = replan(s, FakeRouting(), NOW, TZ, reason="talk")
        s.add(
            ProposalRow(
                kind="unlock_block",
                title="test",
                details={"block_id": "00000000-0000-0000-0000-000000000000"},
                dedupe_key="talk",
                created_at=NOW,
            )
        )
        s.commit()
        first = min(
            s.scalars(select(StudyBlockRow).where(StudyBlockRow.schedule_version_id == version.id)),
            key=lambda b: b.start_at,
        )
        _ = task_id
    with TestClient(create_app(Settings(api_token=TOKEN, database_url=database_url))) as api:

        def say(phrase: str) -> dict[str, object]:
            result: dict[str, object] = api.post(
                "/v1/conversation", json={"text": phrase}, headers=AUTH
            ).json()
            return result

        approved = say("approuve")
        assert approved["changed"] and approved["operations"] == ["decide_proposal"]
        assert not say("refuse")["changed"]  # nothing pending any more
    with sessions() as s:
        assert s.scalars(select(ProposalRow)).one().status == "approved"
        # "c'est fait" relies on the real clock: mark the earliest block as under way now.
        block = s.get(StudyBlockRow, first.id)
        assert block is not None
        now = datetime.now(UTC)
        block.start_at, block.end_at = now - timedelta(minutes=10), now + timedelta(minutes=80)
        s.commit()
    with TestClient(create_app(Settings(api_token=TOKEN, database_url=database_url))) as api:
        done = api.post("/v1/conversation", json={"text": "c'est fait !"}, headers=AUTH).json()
    assert done["changed"] and "marquée comme faite" in done["reply"]
    assert done["operations"] == ["log_work"]
    with sessions() as s:
        assert s.get(StudyBlockRow, first.id).state == "done"  # type: ignore[union-attr]
