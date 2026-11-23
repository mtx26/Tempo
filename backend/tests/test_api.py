from fastapi.testclient import TestClient

from tempo.api.app import create_app
from tempo.config import Settings

TOKEN = "Hq8Wm2Zr5Tx9Lb3Nc7Vd1Kf4Gs6Pj0YeUa"


def make_client() -> TestClient:
    return TestClient(create_app(Settings(api_token=TOKEN)))


def test_health_is_public_and_reports_ok() -> None:
    response = make_client().get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_status_requires_token() -> None:
    assert make_client().get("/v1/status").status_code == 401


def test_status_rejects_wrong_token() -> None:
    response = make_client().get("/v1/status", headers={"Authorization": "Bearer " + "w" * 40})
    assert response.status_code == 401


def test_status_rejects_non_bearer_scheme() -> None:
    response = make_client().get("/v1/status", headers={"Authorization": f"Basic {TOKEN}"})
    assert response.status_code == 401


def test_status_with_valid_token_returns_version_and_timezone() -> None:
    response = make_client().get("/v1/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "Europe/Brussels"
    assert body["version"] == "0.1.0"


def test_openapi_schema_is_not_exposed() -> None:
    assert make_client().get("/openapi.json").status_code == 404


def test_lowercase_bearer_scheme_is_accepted() -> None:
    response = make_client().get("/v1/status", headers={"Authorization": f"bearer {TOKEN}"})
    assert response.status_code == 200


def test_unauthorized_response_never_contains_token() -> None:
    response = make_client().get("/v1/status", headers={"Authorization": f"Basic {TOKEN}"})
    assert TOKEN not in response.text
    assert all(TOKEN not in value for value in response.headers.values())
