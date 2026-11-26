"""LLM model chosen from Home Assistant: listed from the project, probed before being saved,
then used by the conversation without restart (Gemini client faked: no network, no cost)."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tempo.ai.llm import selectable_models
from tempo.api.app import create_app
from tempo.config import Settings
from tempo.const import LLM_MODEL_SETTING
from tempo.db.models import Base
from tempo.db.store import get_setting

pytestmark = pytest.mark.integration
TOKEN = "Hq8Wm2Zr5Tx9Lb3Nc7Vd1Kf4Gs6Pj0YeUa"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LISTED = [
    "publishers/google/models/gemini-3.5-flash-lite",
    "publishers/google/models/gemini-3.5-flash",
    "publishers/google/models/gemini-2.5-flash",
    "publishers/google/models/gemini-3.8-flash-tts",
    "publishers/google/models/gemini-embedding-2",
    "publishers/google/models/gemini-3.1-flash-image",
]


class FakeModels:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def list(self, **_: Any) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=n) for n in LISTED]

    def generate_content(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if kwargs["model"] == "gemini-2.5-flash":
            from google.genai import errors

            raise errors.ClientError(
                404, {"error": {"message": "not found", "status": "NOT_FOUND"}}
            )
        return SimpleNamespace(text="ok", usage_metadata=None)


@pytest.fixture
def sessions(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


def api(database_url: str, models: FakeModels | None) -> TestClient:
    app = create_app(Settings(api_token=SecretStr(TOKEN), database_url=SecretStr(database_url)))
    app.state.gemini = None if models is None else SimpleNamespace(models=models)
    return TestClient(app)


def test_only_text_generation_models_are_selectable() -> None:
    names = [n.rsplit("/", 1)[-1] for n in LISTED]
    assert selectable_models(names) == [
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    ]


def test_list_then_choose_a_model(sessions: sessionmaker[Session], database_url: str) -> None:
    models = FakeModels()
    with api(database_url, models) as client:
        state = client.get("/v1/llm", headers=AUTH).json()
        assert state["enabled"] and state["model"] == "gemini-3.5-flash-lite"
        assert state["default"] == "gemini-3.5-flash-lite"
        assert (
            "gemini-3.5-flash" in state["models"] and "gemini-3.8-flash-tts" not in state["models"]
        )
        chosen = client.put("/v1/llm", headers=AUTH, json={"model": "gemini-3.5-flash"})
        assert chosen.status_code == 200 and chosen.json()["model"] == "gemini-3.5-flash"
        assert models.calls[-1]["model"] == "gemini-3.5-flash"  # probed before saving
        refused = client.put("/v1/llm", headers=AUTH, json={"model": "gemini-2.5-flash"})
        assert refused.status_code == 422 and "inaccessible" in refused.json()["detail"]
        unknown = client.put("/v1/llm", headers=AUTH, json={"model": "gemini-9-ultra"})
        assert unknown.status_code == 422
        assert client.get("/v1/llm").status_code == 401
    with sessions() as s:
        assert get_setting(s, LLM_MODEL_SETTING) == "gemini-3.5-flash"
    with api(database_url, models) as client:  # reset to the deployment default
        reset = client.put("/v1/llm", headers=AUTH, json={"model": None})
        assert reset.json()["model"] == "gemini-3.5-flash-lite"


def test_without_llm_the_state_says_so(sessions: sessionmaker[Session], database_url: str) -> None:
    with api(database_url, None) as client:
        state = client.get("/v1/llm", headers=AUTH).json()
        assert state == {
            "enabled": False,
            "model": None,
            "default": "gemini-3.5-flash-lite",
            "models": [],
        }
        assert client.put("/v1/llm", headers=AUTH, json={"model": "x-model"}).status_code == 409
