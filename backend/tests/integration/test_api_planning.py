"""Home Assistant read routes, technical settings and the operation catalogue over HTTP, on a
REAL PostgreSQL."""

from collections.abc import Iterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
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


def op(api: TestClient, operation: str, /, **arguments: object) -> Response:
    """Business changes go through the operation catalogue (same rules as the assistant)."""
    return api.post(f"/v1/operations/{operation}", json=arguments, headers=AUTH)


def done(api: TestClient, operation: str, /, **arguments: object) -> dict[str, Any]:
    response = op(api, operation, **arguments)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["result"]
    return result


def configure(api: TestClient) -> None:
    for pid, name, lat, lon, aliases in (
        ("home", "Kot", 50.668, 4.612, []),
        ("campus", "Campus", 50.85, 4.35, ["Auditoire A.1"]),
        ("club", "Club", 50.70, 4.60, []),
    ):
        done(
            api,
            "upsert_place",
            place_id=pid,
            name=name,
            latitude=lat,
            longitude=lon,
            aliases=aliases,
        )
    windows = [
        {"weekday": d, "start": "08:00", "end": "20:00", "place_id": "home"} for d in range(7)
    ]
    done(api, "set_weekly_availability", windows=windows)
    patch = {
        "home_place_id": "home",
        "horizon_days": 6,
        "tracker_entity_id": "device_tracker.phone",
    }
    assert api.patch("/v1/settings", json=patch, headers=AUTH).status_code == 204


def add_maths(api: TestClient, minutes: int) -> None:
    done(api, "create_task", title="maths", estimated_minutes=minutes, target_date="2026-12-11")


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
        "/v1/settings",
        "/v1/activities",
        "/v1/proposals",
        "/v1/notifications",
    ):
        assert api.get(path).status_code == 401
    assert api.post("/v1/operations/plan_now", json={}).status_code == 401


def test_business_changes_have_no_rest_route(api: TestClient) -> None:
    """Product rule: every change goes through the catalogue (conversation or operations)."""
    some = "00000000-0000-0000-0000-000000000000"
    for method, path in (
        ("post", "/v1/replan"),
        ("put", "/v1/places/home"),
        ("delete", "/v1/places/home"),
        ("put", "/v1/availability"),
        ("post", "/v1/activities"),
        ("patch", f"/v1/activities/{some}"),
        ("post", f"/v1/blocks/{some}/lock"),
        ("post", f"/v1/proposals/{some}/approve"),
        ("post", "/v1/interpret"),
        ("patch", f"/v1/tasks/{some}"),
    ):
        assert getattr(api, method)(path, headers=AUTH).status_code in (404, 405), path


def test_configuration_round_trip_and_validation(api: TestClient) -> None:
    configure(api)
    places = api.get("/v1/places", headers=AUTH).json()
    assert [p["id"] for p in places] == ["campus", "club", "home"]
    assert places[0]["aliases"] == ["auditoire a.1"]
    assert len(done(api, "get_rules")["weekly_windows"]) == 7
    settings = api.get("/v1/settings", headers=AUTH).json()
    assert settings["home_place_id"] == "home" and settings["horizon_days"] == 6
    bad = {"name": "x", "latitude": 1, "longitude": 1, "aliases": ["Auditoire A.1"]}
    assert "déjà utilisé" in op(api, "upsert_place", place_id="other", **bad).json()["detail"]
    assert op(api, "upsert_place", place_id="Bad-Id", **bad).status_code == 422
    assert "domicile" in op(api, "delete_place", place_id="home").json()["detail"]
    assert (
        api.patch("/v1/settings", json={"home_place_id": "nowhere"}, headers=AUTH).status_code
        == 422
    )
    prefs = {"study_preferences": {"preferred_block_minutes": 7}}
    assert api.patch("/v1/settings", json=prefs, headers=AUTH).status_code == 422
    window = [{"weekday": 0, "start": "08:00", "end": "07:00", "place_id": "home"}]
    assert op(api, "set_weekly_availability", windows=window).status_code == 422


RACE = {
    "kind": "race",
    "title": "10 km",
    "scheduling": "fixed",
    "duration_minutes": 60,
    "start": at(12, 10).isoformat(),
    "place_id": "club",
}


def test_activities(api: TestClient) -> None:
    configure(api)
    created = done(api, "create_event", **RACE)["activity"]
    assert created["window_end"] == at(12, 11).isoformat()
    training = done(
        api,
        "create_event",
        kind="training",
        title="Footing",
        notes="45 min EF",
        scheduling="flexible",
        duration_minutes=45,
        window_start=at(7, 0).isoformat(),
        window_end=at(9, 23).isoformat(),
        preferred_start=at(8, 18).isoformat(),
    )["activity"]
    assert training["notes"] == "45 min EF"
    listed = api.get("/v1/activities", headers=AUTH).json()
    assert {a["kind"] for a in listed} == {"race", "training"}
    lock = op(api, "set_lock", target_id=training["id"], locked=True)
    assert lock.status_code == 422  # not placed yet
    assert op(api, "create_event", **{**RACE, "start": None}).status_code == 422


def test_schedule_and_summary_after_planning(api: TestClient, engine: Engine) -> None:
    configure(api)
    seed_events(engine)
    add_maths(api, 180)
    done(api, "create_event", **RACE)
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


def locked_blocks(engine: Engine) -> list[StudyBlockRow]:
    with Session(engine) as s:
        return list(s.scalars(select(StudyBlockRow).where(StudyBlockRow.locked.is_(True))))


def test_lock_done_and_unlock_blocks(api: TestClient, engine: Engine) -> None:
    configure(api)
    add_maths(api, 180)
    run_replan(engine)
    with Session(engine) as s:
        blocks = list(s.scalars(select(StudyBlockRow)))
    first, second = blocks[0], blocks[1]
    done(api, "set_lock", target_id=f"block:{first.id}", locked=True)
    (locked,) = locked_blocks(engine)
    assert locked.start_at == first.start_at and locked.schedule_version_id is None
    done(api, "log_work", block_id=str(second.id))
    assert op(api, "log_work", block_id=str(second.id)).status_code == 422  # never twice
    with Session(engine) as s:
        task = s.scalars(select(StudyTaskRow)).one()
        assert task.completed_minutes == (second.end_at - second.start_at).total_seconds() // 60
    done(api, "set_lock", target_id=f"block:{locked.id}", locked=False)
    assert locked_blocks(engine) == []


def test_conflict_proposal_approval_flow(api: TestClient, engine: Engine) -> None:
    configure(api)
    seed_events(engine)
    add_maths(api, 90)
    with Session(engine) as s:
        task = s.scalars(select(StudyTaskRow)).one()
        s.add(StudyBlockRow(task_id=task.id, start_at=at(8, 11), end_at=at(8, 12, 30), locked=True))
        s.commit()
    run_replan(engine)
    proposals = api.get("/v1/proposals", headers=AUTH).json()
    assert len(proposals) == 1 and proposals[0]["kind"] == "unlock_block"
    assert api.get("/v1/summary", headers=AUTH).json()["pending_proposals"] == 1
    decided = done(api, "decide_proposal", proposal_id=proposals[0]["id"], approve=True)
    assert decided["proposal"]["status"] == "approved"
    again = op(api, "decide_proposal", proposal_id=proposals[0]["id"], approve=False)
    assert again.status_code == 422 and "déjà" in again.json()["detail"]
    assert locked_blocks(engine) == []
    with Session(engine) as s:
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
    # No routing engine in this API instance: the worker re-plans (request recorded).
    assert done(api, "plan_now")["status"] == "requested"


def test_hardening_limits(api: TestClient) -> None:
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
    many = {"name": "x", "latitude": 1, "longitude": 1, "aliases": [f"a{i}" for i in range(51)]}
    assert op(api, "upsert_place", place_id="x", **many).status_code == 422


def test_locking_the_same_block_twice_creates_one_lock(api: TestClient, engine: Engine) -> None:
    configure(api)
    add_maths(api, 90)
    run_replan(engine)
    with Session(engine) as s:
        block = s.scalars(select(StudyBlockRow)).first()
    assert block is not None
    for _ in range(2):
        done(api, "set_lock", target_id=f"block:{block.id}", locked=True)
    assert len(locked_blocks(engine)) == 1


def test_conversational_questions_are_answered_from_stored_data(
    api: TestClient, engine: Engine
) -> None:
    configure(api)
    seed_events(engine)
    add_maths(api, 150)
    run_replan(engine)

    def ask(text: str) -> str:
        response = api.post("/v1/conversation", json={"text": text}, headers=AUTH)
        assert response.status_code == 200
        result: dict[str, Any] = response.json()
        assert result["fallback"] is True and result["changed"] is False
        return str(result["reply"])

    agenda = ask("Qu'est-ce que j'ai le 07/12/2026 ?")
    assert agenda.startswith("Le 07/12 :") and "cours : Analyse" in agenda and "trajet" in agenda
    assert "Rien de prévu" in ask("programme du 25/12/2026")
    assert "2 h 30" in ask("combien d'heures d'étude il me reste ?")
    assert ask("prochain départ ?")
