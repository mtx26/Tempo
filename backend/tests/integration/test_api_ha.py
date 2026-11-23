"""HTTP API used by the Home Assistant integration (tasks and phone position).

Runs the real FastAPI app against the per-session PostgreSQL test database.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tempo.api.app import create_app
from tempo.config import Settings
from tempo.db.models import AuditEntry, PhonePositionRow, StudyTaskRow

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
        s.execute(delete(AuditEntry).where(AuditEntry.actor == "home_assistant"))
        s.commit()


def create_task(api: TestClient, **overrides: object) -> dict[str, object]:
    body = {"subject": "Analyse", "effort_minutes": 600, "target_date": "2027-01-15", **overrides}
    response = api.post("/v1/tasks", json=body, headers=AUTH)
    assert response.status_code == 201, response.text
    result: dict[str, object] = response.json()
    return result


def test_task_endpoints_require_the_token(api: TestClient) -> None:
    assert api.get("/v1/tasks").status_code == 401
    assert api.post("/v1/tasks", json={}).status_code == 401


def test_created_task_has_soft_target_by_default(api: TestClient) -> None:
    task = create_task(api)
    assert task["target_kind"] == "soft"
    assert task["remaining_minutes"] == 600
    assert task["completed_minutes"] == 0


def test_hard_deadline_must_be_explicit(api: TestClient) -> None:
    task = create_task(api, target_kind="hard")
    assert task["target_kind"] == "hard"


def test_tasks_are_listed(api: TestClient) -> None:
    create_task(api, subject="Analyse")
    create_task(api, subject="Chimie")
    subjects = [t["subject"] for t in api.get("/v1/tasks", headers=AUTH).json()]
    assert subjects == ["Analyse", "Chimie"]


def test_progress_update_changes_remaining_effort(api: TestClient) -> None:
    task = create_task(api)
    response = api.patch(f"/v1/tasks/{task['id']}", json={"completed_minutes": 150}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["remaining_minutes"] == 450


@pytest.mark.parametrize(
    "body",
    [
        {"subject": "", "effort_minutes": 60, "target_date": "2027-01-15"},
        {"subject": "A", "effort_minutes": 0, "target_date": "2027-01-15"},
        {"subject": "A", "effort_minutes": 60, "target_date": "not-a-date"},
        {"subject": "A", "effort_minutes": 60, "target_date": "2027-01-15", "target_kind": "x"},
        {"subject": "A" * 201, "effort_minutes": 60, "target_date": "2027-01-15"},
    ],
)
def test_invalid_tasks_are_rejected(api: TestClient, body: dict[str, object]) -> None:
    assert api.post("/v1/tasks", json=body, headers=AUTH).status_code == 422


def test_unknown_task_returns_404(api: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    response = api.patch(f"/v1/tasks/{missing}", json={"completed_minutes": 1}, headers=AUTH)
    assert response.status_code == 404


def test_task_creation_is_audited(api: TestClient, engine: Engine) -> None:
    task = create_task(api)
    with Session(engine) as s:
        entries = s.query(AuditEntry).filter(AuditEntry.entity_id == str(task["id"])).all()
    assert [e.action for e in entries] == ["task_created"]


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
