"""Read-only iCalendar feed of the published plan (subscribed by a phone calendar app)."""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from icalendar import Calendar
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tempo.api.app import create_app
from tempo.config import Settings
from tempo.db.models import Base
from tempo.planning.service import replan
from tests.integration.test_planning_service import NOW, TZ, seed
from tests.test_engine import FakeRouting

pytestmark = pytest.mark.integration
API_TOKEN = "Hq8Wm2Zr5Tx9Lb3Nc7Vd1Kf4Gs6Pj0YeUa"
FEED_TOKEN = "Zp4Rk8Wn2Qx6Tb1Mv5Jc9Hd3Ls7Gf0YaEu"


@pytest.fixture
def sessions(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


def client(database_url: str, calendar_token: str | None = None) -> TestClient:
    feed = None if calendar_token is None else SecretStr(calendar_token)
    settings = Settings(
        api_token=SecretStr(API_TOKEN), database_url=SecretStr(database_url), calendar_token=feed
    )
    app = create_app(settings)
    app.state.clock = lambda: NOW
    return TestClient(app)


def test_feed_lists_the_published_plan(sessions: sessionmaker[Session], database_url: str) -> None:
    with sessions() as s:
        seed(s)
        replan(s, FakeRouting(), NOW, TZ, reason="test")
        s.commit()
    with client(database_url, calendar_token=FEED_TOKEN) as api:
        response = api.get(f"/calendar/{FEED_TOKEN}.ics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    calendar = Calendar.from_ical(response.content)
    events = list(calendar.walk("VEVENT"))
    assert events, "the published plan has items"
    titles = {str(e["SUMMARY"]) for e in events}
    assert any(t.startswith("Étude : ") for t in titles)  # study blocks
    uids = [str(e["UID"]) for e in events]
    assert len(uids) == len(set(uids)) and all(u.endswith("@tempo") for u in uids)
    for event in events:
        start, end = event.decoded("DTSTART"), event.decoded("DTEND")
        assert end > start
        assert NOW - timedelta(days=15) < end and start < NOW + timedelta(days=61)
    assert str(calendar["X-WR-CALNAME"]) == "Tempo"
    assert API_TOKEN not in response.text and FEED_TOKEN not in response.text


def test_wrong_or_missing_token_reveals_nothing(
    sessions: sessionmaker[Session], database_url: str
) -> None:
    with client(database_url, calendar_token=FEED_TOKEN) as api:
        assert api.get("/calendar/not-the-token.ics").status_code == 404
        assert api.get(f"/calendar/{API_TOKEN}.ics").status_code == 404  # API token is not it
        for _ in range(12):
            api.get("/calendar/guess.ics")
        locked = api.get(f"/calendar/{FEED_TOKEN}.ics")
        assert locked.status_code == 429  # repeated guesses lock the client out
    with client(database_url) as api:  # feed not configured: the route does not exist
        assert api.get(f"/calendar/{FEED_TOKEN}.ics").status_code == 404


def test_calendar_token_must_differ_from_the_api_token() -> None:
    with pytest.raises(ValueError, match="calendar_token"):
        Settings(api_token=SecretStr(API_TOKEN), calendar_token=SecretStr(API_TOKEN))
