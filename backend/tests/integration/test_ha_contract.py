"""Contract test: the REAL Home Assistant integration client (``../tempo-homeassistant``
``custom_components/tempo/api.py``) against the REAL backend (uvicorn + PostgreSQL).

Requires TEMPO_TEST_DATABASE_URL and TEMPO_TEST_HA_REPO (path of the integration repository);
SKIPPED otherwise. Catches any drift between the two repositories (paths, payloads, statuses).
"""

import asyncio
import importlib.util
import os
import socket
import sys
import threading
import time
import types
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tempo.api.app import create_app
from tempo.config import Settings
from tempo.db.models import Base

HA_REPO = os.environ.get("TEMPO_TEST_HA_REPO")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not HA_REPO, reason="TEMPO_TEST_HA_REPO not set: contract not tested"),
]
TOKEN = "Hq8Wm2Zr5Tx9Lb3Nc7Vd1Kf4Gs6Pj0YeUa"


def load_client_module() -> Any:
    assert HA_REPO is not None
    folder = Path(HA_REPO) / "custom_components" / "tempo"
    package = types.ModuleType("tempo_ha")
    package.__path__ = [str(folder)]
    sys.modules["tempo_ha"] = package
    for name in ("const", "api"):
        spec = importlib.util.spec_from_file_location(f"tempo_ha.{name}", folder / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"tempo_ha.{name}"] = module
        spec.loader.exec_module(module)
    return sys.modules["tempo_ha.api"]


@pytest.fixture
def server(database_url: str, engine: Engine) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = create_app(Settings(api_token=TOKEN, database_url=database_url))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not instance.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    instance.should_exit = True
    thread.join(timeout=10)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


ENGINE: list[Engine] = []


def test_every_client_call_matches_the_backend(server: str, engine: Engine) -> None:
    ENGINE[:] = [engine]
    api_module = load_client_module()
    import aiohttp

    async def scenario() -> None:
        async with aiohttp.ClientSession() as http:
            bad = api_module.TempoApiClient(http, server, "x" * 40)
            with pytest.raises(api_module.TempoAuthError):
                await bad.status()
            client = api_module.TempoApiClient(http, server, TOKEN)
            assert (await client.health())["status"] == "ok"
            assert "version" in await client.status()
            op = client.operation
            await op(
                "upsert_place",
                {"place_id": "home", "name": "Kot", "latitude": 50.67, "longitude": 4.61},
            )
            await op(
                "upsert_place",
                {
                    "place_id": "club",
                    "name": "Club",
                    "latitude": 50.7,
                    "longitude": 4.6,
                    "aliases": ["Stade"],
                },
            )
            assert {p["id"] for p in await client.places()} == {"home", "club"}
            await op(
                "set_weekly_availability",
                {
                    "windows": [
                        {"weekday": d, "start": "08:00", "end": "20:00", "place_id": "home"}
                        for d in range(7)
                    ]
                },
            )
            rules = await op("get_rules", {})
            assert len(rules["result"]["weekly_windows"]) == 7
            await op("update_settings", {"home_place_id": "home"})
            # technical setting pushed by the options flow
            await client.update_settings({"tracker_entity_id": "device_tracker.phone"})
            settings = await client.context()
            assert settings["home_place_id"] == "home"
            assert settings["tracker_entity_id"] == "device_tracker.phone"
            soon = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
            task = (
                await op(
                    "create_task", {"title": "maths", "estimated_minutes": 120, "target_date": soon}
                )
            )["result"]["task"]
            await op("log_work", {"task_id": task["id"], "minutes": 30})
            tasks = await client.tasks()
            assert [t["id"] for t in tasks] == [task["id"]]
            assert tasks[0]["remaining_minutes"] == 90 and tasks[0]["hard_deadline"] is False
            start = datetime.now(UTC) + timedelta(days=2)
            concert = await op(
                "create_event",
                {
                    "title": "Concert",
                    "scheduling": "fixed",
                    "start": start.isoformat(),
                    "duration_minutes": 90,
                    "place_id": "club",
                },
            )
            await client.post_location(
                {
                    "entity_id": "device_tracker.phone",
                    "latitude": 50.67,
                    "longitude": 4.61,
                    "accuracy_m": 12.0,
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            )
            items = await client.schedule(datetime.now(UTC), datetime.now(UTC) + timedelta(days=7))
            assert any(i["type"] == "personal" and i["title"] == "Concert" for i in items)
            summary = await client.summary()
            assert summary["remaining_study_minutes"] == 90
            assert await client.proposals() == []
            assert isinstance(await client.notifications(), list)
            assert summary["to_confirm"] == []
            # Conversation without LLM: deterministic fallback, real data, nothing invented.
            question = await client.conversation("combien d'heures d'étude il me reste ?", "c1")
            assert question["used_llm"] is False and "1 h 30" in question["reply"]
            replan = await client.conversation("replanifie", "c1")
            assert replan["changed"] is True
            with pytest.raises(api_module.TempoNotFoundError):
                await op("no_such_operation", {})
            with pytest.raises(api_module.TempoApiError):
                await op("create_task", {"title": ""})
            with pytest.raises(api_module.TempoApiError):
                await op("delete_place", {"place_id": "home"})  # still the home place
            with pytest.raises(api_module.TempoApiError, match="Concert"):
                await op("delete_place", {"place_id": "club"})  # still used: never silent
            await op("cancel_event", {"activity_id": concert["result"]["activity"]["id"]})
            await op("delete_place", {"place_id": "club"})

    asyncio.run(scenario())
