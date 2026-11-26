"""M5b on a REAL PostgreSQL: coach .docx plans imported, updated without duplicates, confirmed
and planned with order/recovery constraints (synthetic fixtures)."""

import itertools
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tempo.api.app import create_app
from tempo.config import Settings
from tempo.db.models import ActivityRow, AvailabilityRow, Base, NotificationRow, PlaceRow
from tempo.db.store import set_setting
from tempo.planning.service import replan
from tempo.sync.coach_import import apply_weekly_plan
from tempo.sync.coach_plan import parse_plan_docx
from tempo.sync.training_mail import MailMessage
from tempo.worker.jobs import training_mail_job
from tests.docx_fixtures import EXAMPLE_WEEK, THEME, plan_docx
from tests.test_engine import FakeRouting

pytestmark = pytest.mark.integration
TZ = ZoneInfo("Europe/Brussels")
TOKEN = "Hq8Wm2Zr5Tx9Lb3Nc7Vd1Kf4Gs6Pj0YeUa"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TODAY = date(2026, 11, 20)  # Friday before the plan's week (23..29/11/2026)


@pytest.fixture
def sessions(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


def import_plan(
    sessions: sessionmaker[Session], data: bytes, origin: str, today: date = TODAY
) -> object:
    with sessions() as s:
        report = apply_weekly_plan(
            s, parse_plan_docx(data, today), tz=TZ, today=today, origin=origin
        )
        s.commit()
        return report


def rows(sessions: sessionmaker[Session]) -> list[ActivityRow]:
    with sessions() as s:
        return list(s.scalars(select(ActivityRow).order_by(ActivityRow.preferred_day)))


def test_import_then_resend_unchanged_modified_and_shortened(
    sessions: sessionmaker[Session],
) -> None:
    import_plan(sessions, plan_docx(), "m1")
    first = rows(sessions)
    assert [(r.title, r.status, r.duration_minutes) for r in first] == [
        ("Test VMA", "to_confirm", None),
        ("Endurance Fondamentale", "active", 75),
        ("Sortie Longue", "active", 85),
    ]
    assert {r.sequence_group for r in first} == {"coach_plan:2026-11-23"}
    assert [r.sequence_index for r in first] == [0, 1, 2]

    import_plan(sessions, plan_docx(), "m2")  # identical re-send: nothing duplicated
    assert [r.id for r in rows(sessions)] == [r.id for r in first]

    week = {k: list(v) for k, v in EXAMPLE_WEEK.items()}
    week[THEME][4] = "Endurance fondamentale 1h05'"
    week[THEME][6] = ""
    week["Objectif"][6] = ""
    week["Contenu"][6] = ""
    import_plan(sessions, plan_docx(week=week), "m3")
    after = rows(sessions)
    assert len(after) == 2
    assert after[1].id == first[1].id and after[1].duration_minutes == 65  # updated in place
    with sessions() as s:
        kinds = [n.kind for n in s.scalars(select(NotificationRow))]
    assert kinds.count("coach_plan") == 2 and "coach_plan_confirm" in kinds


def test_user_edits_are_never_overwritten(sessions: sessionmaker[Session]) -> None:
    import_plan(sessions, plan_docx(), "m1")
    with sessions() as s:
        row = s.scalars(select(ActivityRow).where(ActivityRow.duration_minutes == 75)).one()
        row.notes, row.user_modified = "ma version", True
        s.commit()
    week = {k: list(v) for k, v in EXAMPLE_WEEK.items()}
    week[THEME][4] = "Endurance fondamentale 1h05'"
    import_plan(sessions, plan_docx(week=week), "m2")
    edited = next(r for r in rows(sessions) if r.preferred_day == date(2026, 11, 27))
    assert edited.notes == "ma version" and edited.duration_minutes == 75
    with sessions() as s:
        assert any(n.kind == "coach_plan_conflict" for n in s.scalars(select(NotificationRow)))


def test_past_days_are_skipped(sessions: sessionmaker[Session]) -> None:
    import_plan(sessions, plan_docx(), "m1", today=date(2026, 11, 28))
    assert [r.preferred_day for r in rows(sessions)] == [date(2026, 11, 29)]


def test_confirm_api_and_planning_respects_order_and_recovery(
    database_url: str, sessions: sessionmaker[Session]
) -> None:
    import_plan(sessions, plan_docx(), "m1")
    with sessions() as s:
        s.add(PlaceRow(id="home", name="Kot", latitude=50.67, longitude=4.61))
        s.flush()
        for weekday in range(7):
            s.add(AvailabilityRow(weekday=weekday, start_minute=480, end_minute=1200))
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
        set_setting(s, "horizon_days", 10)
        s.commit()
    with TestClient(create_app(Settings(api_token=TOKEN, database_url=database_url))) as api:
        summary = api.post("/v1/operations/get_summary", json={}, headers=AUTH).json()
        assert [a["kind"] for a in summary["result"]["to_confirm"]] == ["training"]
        vma = next(
            a
            for a in api.post("/v1/operations/list_activities", json={}, headers=AUTH).json()[
                "result"
            ]["activities"]
            if a["status"] == "to_confirm"
        )
        assert vma["preferred_day"] == "2026-11-25"

        def confirm(minutes: int) -> Any:
            return api.post(
                "/v1/operations/confirm_activity",
                json={"activity_id": vma["id"], "duration_minutes": minutes},
                headers=AUTH,
            )

        assert confirm(0).status_code == 422  # never a guessed or empty duration
        ok = confirm(50)
        assert ok.status_code == 200 and ok.json()["result"]["activity"]["status"] == "active"
        again = confirm(50)
        assert again.status_code == 422 and "déjà" in again.json()["detail"]
    with sessions() as s:
        now = datetime(2026, 11, 22, 18, tzinfo=TZ).astimezone(UTC)
        version = replan(s, FakeRouting(), now, TZ, reason="coach plan")
        s.commit()
        assert version.state == "active", version.summary["issues"]
    placed = sorted(
        (r.sequence_index, r.planned_start_at) for r in rows(sessions) if r.planned_start_at
    )
    assert [i for i, _ in placed] == [0, 1, 2]
    days = [start.astimezone(TZ).date() for _, start in placed]
    assert days == [date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 29)]
    assert all((b - a) >= timedelta(days=2) for a, b in itertools.pairwise(days))


def test_coach_email_with_docx_attachment(sessions: sessionmaker[Session]) -> None:
    message = MailMessage(
        "<plan@coach>",
        "coach@club.be",
        "plan",
        "Confirme la réception",
        (("Plan Matis (1).docx", plan_docx()),),
    )
    now = datetime(2026, 11, 20, 8, tzinfo=UTC)
    stats = training_mail_job(sessions, _Mail([message]), now, TZ, None)
    assert stats["trainings"] == 3 and stats["failed"] == 0
    assert len(rows(sessions)) == 3
    broken = MailMessage("<bad@coach>", "coach@club.be", "plan", "x", (("p.docx", b"junk"),))
    training_mail_job(sessions, _Mail([broken]), now, TZ, None)
    with sessions() as s:
        assert any(n.kind == "coach_plan_unreadable" for n in s.scalars(select(NotificationRow)))


class _Mail:
    def __init__(self, messages: list[MailMessage]) -> None:
        self.messages = messages

    def fetch(self, since: date) -> list[MailMessage]:
        return self.messages
