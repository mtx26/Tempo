"""Tasks through the operation catalogue and the phone position pushed by Home Assistant.

Runs the real FastAPI app against the per-session PostgreSQL test database.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from tempo.api.app import create_app
from tempo.config import Settings
from tempo.db.models import AuditEntry, JobRunRow, PhonePositionRow, StudyTaskRow

pytestmark = pytest.mark.integration

TOKEN = "Hq8Wm2Zr5Tx9Lb3Nc7Vd1Kf4Gs6Pj0YeUa"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api(database_url: str, engine: Engine) -> Iterator[TestClient]:
    settings = Settings(api_token=TOKEN, database_url=database_url)
    with TestClient(create_app(settings)) as client:
        yield client
    with Session(engine) as s:
        s.execute(delete(StudyTaskRow))
        s.execute(delete(PhonePositionRow))
        s.execute(delete(AuditEntry).where(AuditEntry.actor.in_(("home_assistant", "api"))))
        s.commit()


def create_task(api: TestClient, **overrides: object) -> dict[str, object]:
    """Tasks are created through the operation catalogue (never a dedicated REST route)."""
    body = {
        "title": "Analyse",
        "estimated_minutes": 600,
        "splittable": True,
        "target_date": "2027-01-15",
        **overrides,
    }
    response = api.post("/v1/operations/create_task", json=body, headers=AUTH)
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()["result"]["task"]
    return result


def tasks(api: TestClient) -> list[dict[str, Any]]:
    response = api.post("/v1/operations/list_tasks", json={}, headers=AUTH)
    assert response.status_code == 200, response.text
    found: list[dict[str, Any]] = response.json()["result"]["tasks"]
    return found


def test_task_operations_require_the_token(api: TestClient) -> None:
    assert api.post("/v1/operations/list_tasks", json={}).status_code == 401
    assert api.post("/v1/operations/create_task", json={}).status_code == 401


def test_diagnostics_is_private_and_reuses_postgresql_stats(api: TestClient) -> None:
    assert api.get("/v1/diagnostics").status_code == 401
    response = api.get("/v1/diagnostics", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["postgresql"]["status"] == "ok"
    assert body["postgresql"]["connections"] >= 1
    assert body["postgresql"]["waiting_locks"] >= 0
    assert body["motis"]["status"] == "unconfigured"
    assert body["worker"]["status"] == "missing_heartbeat"


def test_diagnostics_distinguishes_upstream_state_and_redacts_backend_details(
    database_url: str, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 11, 27, 17, tzinfo=UTC)
    old = now - timedelta(minutes=6)
    private = "private-upstream-data"
    feed_name = "tec/gtfs-rt-trip-updates"

    def health(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "motis.test":
            raise httpx2.ConnectError("unreachable", request=request)
        return httpx2.Response(
            200,
            json={
                "internal": private,
                "feeds": {
                    feed_name: {"state": "stale", "age_seconds": 91, "last_error": private},
                    private: {"state": "operational"},
                },
            },
        )

    client = httpx2.Client
    monkeypatch.setattr(
        "tempo.api.app.httpx2.Client",
        lambda **kw: client(**kw, transport=httpx2.MockTransport(health)),
    )
    role = f"tempo_diag_{uuid.uuid4().hex[:10]}"
    password = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
        conn.execute(text(f"GRANT SELECT ON job_run, app_setting TO {role}"))
        with Session(bind=conn) as session:
            session.add(
                JobRunRow(
                    name="reminders",
                    last_started_at=old,
                    last_finished_at=old,
                    last_status="error",
                    last_error=f"JobError: {private}",
                    last_success_at=old - timedelta(minutes=1),
                )
            )
            session.flush()
    restricted = make_url(database_url).set(username=role, password=password)
    app = create_app(
        Settings(
            api_token=TOKEN,
            database_url=restricted.render_as_string(hide_password=False),
            motis_url="http://motis.test",
            rt_relay_url="http://relay.test",
        )
    )
    app.state.clock = lambda: now
    try:
        with TestClient(app) as api:
            response = api.get("/v1/diagnostics", headers=AUTH)
            assert response.status_code == 200
            assert private not in response.text
            body = response.json()
            assert not body["motis"]["reachable"] and not body["motis"]["operational"]
            relay = body["gtfs_realtime"]
            assert relay["reachable"] and not relay["operational"] and relay["freshness"] == "stale"
            assert list(relay["feeds"]) == [feed_name]
            assert body["worker"]["status"] == "stale_heartbeat"
            job = body["postgresql"]["jobs"][0]
            assert job["last_error"] == "JobError" and job["last_success"]
    finally:
        with engine.begin() as conn:
            conn.execute(delete(JobRunRow).where(JobRunRow.name == "reminders"))
            # The role name is generated from a UUID above.
            conn.execute(text(f"REVOKE SELECT ON job_run, app_setting FROM {role}"))  # noqa: S608
            conn.execute(text(f"DROP ROLE {role}"))


def test_created_task_has_soft_target_by_default(api: TestClient) -> None:
    task = create_task(api)
    assert task["hard_deadline"] is False
    assert task["remaining_minutes"] == 600
    assert task["done_minutes"] == 0


def test_hard_deadline_must_be_explicit(api: TestClient) -> None:
    assert tasks(api) == []
    task = create_task(api, hard_deadline=True)
    assert task["hard_deadline"] is True
    assert tasks(api)[0]["hard_deadline"] is True


def test_tasks_are_listed(api: TestClient) -> None:
    create_task(api, title="Analyse")
    create_task(api, title="Chimie")
    subjects = [t["title"] for t in tasks(api)]
    assert subjects == ["Analyse", "Chimie"]


def test_progress_update_changes_remaining_effort(api: TestClient) -> None:
    task = create_task(api)
    response = api.post(
        "/v1/operations/log_work", json={"task_id": task["id"], "minutes": 150}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    assert tasks(api)[0]["remaining_minutes"] == 450


@pytest.mark.parametrize(
    "body",
    [
        {"title": "", "estimated_minutes": 60, "splittable": True, "target_date": "2027-01-15"},
        {"title": "A", "estimated_minutes": 0, "splittable": True, "target_date": "2027-01-15"},
        {"title": "A", "estimated_minutes": 60, "splittable": True, "target_date": "not-a-date"},
        {
            "title": "A",
            "estimated_minutes": 60,
            "splittable": True,
            "target_date": "2027-01-15",
            "priority": 9,
        },
        {
            "title": "A" * 201,
            "estimated_minutes": 60,
            "splittable": True,
            "target_date": "2027-01-15",
        },
    ],
)
def test_invalid_tasks_are_rejected(api: TestClient, body: dict[str, object]) -> None:
    assert api.post("/v1/operations/create_task", json=body, headers=AUTH).status_code == 422


def test_unknown_task_is_rejected(api: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    response = api.post(
        "/v1/operations/log_work", json={"task_id": missing, "minutes": 1}, headers=AUTH
    )
    assert response.status_code == 422


def test_task_creation_is_audited(api: TestClient, engine: Engine) -> None:
    create_task(api)
    with Session(engine) as s:
        entries = s.query(AuditEntry).filter(AuditEntry.actor == "api").all()
    assert [e.action for e in entries] == ["op:create_task"]


POSITION = {
    "entity_id": "device_tracker.phone",
    "latitude": 50.8455,
    "longitude": 4.3571,
    "accuracy_m": 15.0,
    "observed_at": "2026-12-01T07:00:00+00:00",
}


def test_phone_position_is_stored_as_latest_only(api: TestClient, engine: Engine) -> None:
    assert api.post("/v1/location", json=POSITION, headers=AUTH).status_code == 204
    newer = {**POSITION, "latitude": 50.85, "observed_at": "2026-12-01T07:05:00+00:00"}
    assert api.post("/v1/location", json=newer, headers=AUTH).status_code == 204
    with Session(engine) as s:
        rows = s.query(PhonePositionRow).all()
    assert len(rows) == 1
    assert rows[0].latitude == 50.85


def test_older_position_does_not_overwrite_newer(api: TestClient, engine: Engine) -> None:
    newer = {**POSITION, "observed_at": "2026-12-01T07:05:00+00:00"}
    api.post("/v1/location", json=newer, headers=AUTH)
    older = {**POSITION, "latitude": 10.0}
    assert api.post("/v1/location", json=older, headers=AUTH).status_code == 204
    with Session(engine) as s:
        assert s.query(PhonePositionRow).one().latitude == POSITION["latitude"]


@pytest.mark.parametrize(
    "patch",
    [
        {"latitude": 95},
        {"longitude": -200},
        {"accuracy_m": -1},
        {"observed_at": "2026-12-01T07:00:00"},
        {"entity_id": "sensor.not_a_tracker"},
    ],
)
def test_invalid_positions_are_rejected(api: TestClient, patch: dict[str, object]) -> None:
    assert api.post("/v1/location", json={**POSITION, **patch}, headers=AUTH).status_code == 422
