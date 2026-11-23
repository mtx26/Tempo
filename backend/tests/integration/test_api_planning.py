"""Planning API (calendar, sensors, configuration, approvals) on a REAL PostgreSQL."""

from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tempo.api.app import create_app
from tempo.config import Settings
from tempo.db.ics_repository import get_or_create_source
from tempo.db.models import Base, FixedEventRow, StudyBlockRow, StudyTaskRow
from tempo.domain.models import EventStatus
from tempo.planning.service import get_setting, replan
from tests.test_engine import FakeRouting

pytestmark = pytest.mark.integration
TOKEN = "Hq8Wm2Zr5Tx9Lb3Nc7Vd1Kf4Gs6Pj0YeUa"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TZ = ZoneInfo("Europe/Brussels")
NOW = datetime(2026, 12, 7, 6, tzinfo=TZ)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 12, day, hour, minute, tzinfo=TZ)


@pytest.fixture
def api(database_url: str, engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(api_token=TOKEN, database_url=database_url))) as client:
        yield client
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


def configure(api: TestClient) -> None:
    for pid, name, lat, lon, aliases in (
        ("home", "Kot", 50.668, 4.612, []),
        ("campus", "Campus", 50.85, 4.35, ["Auditoire A.1"]),
        ("club", "Club", 50.70, 4.60, []),
    ):
        body = {"name": name, "latitude": lat, "longitude": lon, "aliases": aliases}
        assert api.put(f"/v1/places/{pid}", json=body, headers=AUTH).status_code == 204
    windows = [
        {"weekday": d, "start": "08:00", "end": "20:00", "place_id": "home"} for d in range(7)
    ]
    assert api.put("/v1/availability", json=windows, headers=AUTH).status_code == 204
    patch = {
        "home_place_id": "home",
        "horizon_days": 6,
        "tracker_entity_id": "device_tracker.phone",
    }
    assert api.patch("/v1/settings", json=patch, headers=AUTH).status_code == 204


def seed_events(engine: Engine) -> None:
    with Session(engine) as s:
        source = get_or_create_source(s, "université")
        for d in (7, 8):
            s.add(
                FixedEventRow(
                    source_id=source.id,
                    uid=f"c{d}",
                    start_at=at(d, 10),
                    end_at=at(d, 12),
                    summary="Analyse",
                    location="auditoire  a.1",
                    status=EventStatus.CONFIRMED,
                    first_seen_at=NOW,
                    updated_at=NOW,
                )
            )
        s.commit()


def run_replan(engine: Engine) -> None:
    with Session(engine) as s:
        replan(s, FakeRouting(), NOW, TZ, reason="test")
        s.commit()


def test_every_route_requires_the_token(api: TestClient) -> None:
    for path in (
        "/v1/summary",
        "/v1/places",
        "/v1/availability",
        "/v1/settings",
        "/v1/activities",
        "/v1/proposals",
        "/v1/notifications",
    ):
        assert api.get(path).status_code == 401
    assert api.post("/v1/replan").status_code == 401


def test_configuration_round_trip_and_validation(api: TestClient) -> None:
    configure(api)
    places = api.get("/v1/places", headers=AUTH).json()
    assert [p["id"] for p in places] == ["campus", "club", "home"]
    assert places[0]["aliases"] == ["auditoire a.1"]
    assert len(api.get("/v1/availability", headers=AUTH).json()) == 7
    settings = api.get("/v1/settings", headers=AUTH).json()
    assert settings["home_place_id"] == "home" and settings["horizon_days"] == 6
    bad = {"name": "x", "latitude": 1, "longitude": 1, "aliases": ["Auditoire A.1"]}
    assert api.put("/v1/places/other", json=bad, headers=AUTH).status_code == 409
    assert api.put("/v1/places/Bad-Id", json=bad, headers=AUTH).status_code == 422
    assert api.delete("/v1/places/home", headers=AUTH).status_code == 409
    assert (
        api.patch("/v1/settings", json={"home_place_id": "nowhere"}, headers=AUTH).status_code
        == 422
    )
    prefs = {"study_preferences": {"preferred_block_minutes": 7}}
    assert api.patch("/v1/settings", json=prefs, headers=AUTH).status_code == 422
    window = [{"weekday": 0, "start": "08:00", "end": "07:00", "place_id": "home"}]
    assert api.put("/v1/availability", json=window, headers=AUTH).status_code == 422


def test_activities(api: TestClient) -> None:
    configure(api)
    race = {
        "kind": "race",
        "title": "10 km",
        "duration_minutes": 60,
        "start": at(12, 10).isoformat(),
        "place_id": "club",
    }
    created = api.post("/v1/activities", json=race, headers=AUTH)
    assert created.status_code == 201, created.text
    assert created.json()["latest_end"] == at(12, 11).isoformat()
    training = {
        "kind": "training",
        "title": "Footing",
        "notes": "45 min EF",
        "duration_minutes": 45,
        "start": at(8, 18).isoformat(),
    }
    t = api.post("/v1/activities", json=training, headers=AUTH).json()
    assert t["earliest_start"].startswith("2026-12-07") and t["notes"] == "45 min EF"
    lock = api.patch(f"/v1/activities/{t['id']}", json={"locked": True}, headers=AUTH)
    assert lock.status_code == 409  # not placed yet
    assert api.post("/v1/activities", json={**race, "start": None}, headers=AUTH).status_code == 422


def test_schedule_and_summary_after_planning(api: TestClient, engine: Engine) -> None:
    configure(api)
    seed_events(engine)
    api.post(
        "/v1/tasks",
        json={"subject": "maths", "effort_minutes": 180, "target_date": "2026-12-11"},
        headers=AUTH,
    )
    api.post(
        "/v1/activities",
        json={
            "kind": "race",
            "title": "10 km",
            "duration_minutes": 60,
            "start": at(12, 10).isoformat(),
            "place_id": "club",
        },
        headers=AUTH,
    )
    run_replan(engine)
    params = {"start": at(7, 0).isoformat(), "end": at(13, 0).isoformat()}
    items = api.get("/v1/schedule", params=params, headers=AUTH).json()
    types = {i["type"] for i in items}
    assert {"class", "study", "race", "travel"} <= types
    assert next(i for i in items if i["type"] == "class")["place_id"] == "campus"
    assert (
        sum(
            (datetime.fromisoformat(i["end"]) - datetime.fromisoformat(i["start"])).total_seconds()
            // 60
            for i in items
            if i["type"] == "study"
        )
        == 180
    )
    summary = api.get("/v1/summary", headers=AUTH).json()
    assert summary["plan_status"] == "complete"
    assert summary["remaining_study_minutes"] == 180
    bad = {"start": at(13, 0).isoformat(), "end": at(7, 0).isoformat()}
    assert api.get("/v1/schedule", params=bad, headers=AUTH).status_code == 422


def test_lock_done_and_unlock_blocks(api: TestClient, engine: Engine) -> None:
    configure(api)
    api.post(
        "/v1/tasks",
        json={"subject": "maths", "effort_minutes": 180, "target_date": "2026-12-11"},
        headers=AUTH,
    )
    run_replan(engine)
    with Session(engine) as s:
        blocks = list(s.scalars(select(StudyBlockRow)))
    first, second = blocks[0], blocks[1]
    assert api.post(f"/v1/blocks/{first.id}/lock", headers=AUTH).status_code == 204
    with Session(engine) as s:
        locked = s.scalars(select(StudyBlockRow).where(StudyBlockRow.locked.is_(True))).one()
        assert locked.start_at == first.start_at and locked.schedule_version_id is None
    assert api.post(f"/v1/blocks/{second.id}/done", headers=AUTH).status_code == 204
    assert api.post(f"/v1/blocks/{second.id}/done", headers=AUTH).status_code == 204  # idempotent
    with Session(engine) as s:
        task = s.scalars(select(StudyTaskRow)).one()
        assert task.completed_minutes == (second.end_at - second.start_at).total_seconds() // 60
    assert api.post(f"/v1/blocks/{locked.id}/unlock", headers=AUTH).status_code == 204
    with Session(engine) as s:
        assert (
            s.scalars(select(StudyBlockRow).where(StudyBlockRow.locked.is_(True))).first() is None
        )


def test_conflict_proposal_approval_flow(api: TestClient, engine: Engine) -> None:
    configure(api)
    seed_events(engine)
    api.post(
        "/v1/tasks",
        json={"subject": "maths", "effort_minutes": 90, "target_date": "2026-12-11"},
        headers=AUTH,
    )
    with Session(engine) as s:
        task = s.scalars(select(StudyTaskRow)).one()
        s.add(StudyBlockRow(task_id=task.id, start_at=at(8, 11), end_at=at(8, 12, 30), locked=True))
        s.commit()
    run_replan(engine)
    proposals = api.get("/v1/proposals", headers=AUTH).json()
    assert len(proposals) == 1 and proposals[0]["kind"] == "unlock_block"
    assert api.get("/v1/summary", headers=AUTH).json()["pending_proposals"] == 1
    decided = api.post(f"/v1/proposals/{proposals[0]['id']}/approve", headers=AUTH)
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    assert api.post(f"/v1/proposals/{proposals[0]['id']}/reject", headers=AUTH).status_code == 409
    with Session(engine) as s:
        assert (
            s.scalars(select(StudyBlockRow).where(StudyBlockRow.locked.is_(True))).first() is None
        )
        assert get_setting(s, "replan_requested") is not None


def test_notifications_and_replan_request(api: TestClient, engine: Engine) -> None:
    configure(api)
    with Session(engine) as s:
        source = get_or_create_source(s, "université")
        s.add(
            FixedEventRow(
                source_id=source.id,
                uid="x",
                start_at=at(9, 14),
                end_at=at(9, 16),
                summary="TP",
                location="Salle inconnue",
                status=EventStatus.CONFIRMED,
                first_seen_at=NOW,
                updated_at=NOW,
            )
        )
        s.commit()
    run_replan(engine)
    notes = api.get("/v1/notifications", headers=AUTH).json()
    target = next(n for n in notes if "Salle inconnue" in n["message"])
    assert api.post(f"/v1/notifications/{target['id']}/ack", headers=AUTH).status_code == 204
    remaining = api.get("/v1/notifications", headers=AUTH).json()
    assert all(n["id"] != target["id"] for n in remaining)
    assert api.post("/v1/replan", headers=AUTH).status_code == 202


def test_hardening_limits(api: TestClient, engine: Engine) -> None:
    configure(api)
    assert api.get("/v1/notifications", params={"limit": -1}, headers=AUTH).status_code == 422
    assert api.get("/v1/notifications", params={"limit": 500}, headers=AUTH).status_code == 422
    huge = {
        "study_preferences": {
            "granularity_minutes": 1,
            "min_block_minutes": 1,
            "preferred_block_minutes": 1,
            "max_block_minutes": 1,
        }
    }
    assert api.patch("/v1/settings", json=huge, headers=AUTH).status_code == 422
    far = {
        "kind": "race",
        "title": "x",
        "duration_minutes": 60,
        "start": "2099-01-01T10:00:00+01:00",
    }
    assert api.post("/v1/activities", json=far, headers=AUTH).status_code == 422
    many = {"name": "x", "latitude": 1, "longitude": 1, "aliases": [f"a{i}" for i in range(51)]}
    assert api.put("/v1/places/x", json=many, headers=AUTH).status_code == 422


def test_locking_the_same_block_twice_creates_one_lock(api: TestClient, engine: Engine) -> None:
    configure(api)
    api.post(
        "/v1/tasks",
        json={"subject": "maths", "effort_minutes": 90, "target_date": "2026-12-11"},
        headers=AUTH,
    )
    run_replan(engine)
    with Session(engine) as s:
        block = s.scalars(select(StudyBlockRow)).first()
    assert block is not None
    for _ in range(2):
        assert api.post(f"/v1/blocks/{block.id}/lock", headers=AUTH).status_code == 204
    with Session(engine) as s:
        assert (
            len(list(s.scalars(select(StudyBlockRow).where(StudyBlockRow.locked.is_(True))))) == 1
        )
