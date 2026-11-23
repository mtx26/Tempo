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
            await client.put_place(
                "home", {"name": "Kot", "latitude": 50.67, "longitude": 4.61, "aliases": []}
            )
            await client.put_place(
                "club",
                {"name": "Club", "latitude": 50.7, "longitude": 4.6, "aliases": ["Stade"]},
            )
            assert {p["id"] for p in await client.places()} == {"home", "club"}
            await client.put_availability(
                [
                    {"weekday": d, "start": "08:00", "end": "20:00", "place_id": "home"}
                    for d in range(7)
                ]
            )
            assert len(await client.availability()) == 7
            await client.patch_settings(
                {"home_place_id": "home", "tracker_entity_id": "device_tracker.phone"}
            )
            assert (await client.settings())["home_place_id"] == "home"
            soon = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
            task = await client.create_task(
                {"subject": "maths", "effort_minutes": 120, "target_date": soon}
            )
            assert task["target_kind"] == "soft"
            updated = await client.update_task(task["id"], {"completed_minutes": 30})
            assert updated["remaining_minutes"] == 90
            assert [t["id"] for t in await client.tasks()] == [task["id"]]
            start = datetime.now(UTC) + timedelta(days=2)
            race = await client.create_activity(
                {
                    "kind": "race",
                    "title": "10 km",
                    "duration_minutes": 60,
                    "start": start.isoformat(),
                    "place_id": "club",
                }
            )
            assert race["kind"] == "race"
            await client.update_activity(race["id"], {"notes": "dossard 12"})
            assert (await client.activities())[0]["notes"] == "dossard 12"
            await client.post_location(
                {
                    "entity_id": "device_tracker.phone",
                    "latitude": 50.67,
                    "longitude": 4.61,
                    "accuracy_m": 12.0,
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            )
            assert "requested_at" in await client.replan()
            items = await client.schedule(datetime.now(UTC), datetime.now(UTC) + timedelta(days=7))
            assert any(i["type"] == "race" for i in items)
            summary = await client.summary()
            assert summary["remaining_study_minutes"] == 90
            assert await client.proposals() == []
            assert isinstance(await client.notifications(), list)
            answer = await client.interpret("replanifie")
            assert answer["applied"] and answer["action"] == "replan"
            question = await client.interpret("combien d'heures d'étude il me reste ?")
            assert question["action"] == "answer" and "1 h 30" in question["summary"]
            # A coach-plan session without duration is confirmed through the client.
            from zoneinfo import ZoneInfo

            from sqlalchemy.orm import Session as OrmSession

            from tempo.sync.coach_import import apply_weekly_plan
            from tempo.sync.coach_plan import parse_plan_docx
            from tests.docx_fixtures import plan_docx

            monday = (datetime.now(UTC) + timedelta(days=7 - datetime.now(UTC).weekday())).date()
            title = f"Semaine du {monday.day:02d}/{monday.month:02d} au "
            sunday = monday + timedelta(days=6)
            title += f"{sunday.day:02d}/{sunday.month:02d}/{sunday.year}"
            with OrmSession(ENGINE[0]) as orm:
                apply_weekly_plan(
                    orm,
                    parse_plan_docx(plan_docx(title=title), monday - timedelta(days=1)),
                    tz=ZoneInfo("Europe/Brussels"),
                    today=monday - timedelta(days=1),
                    origin="contract",
                )
                orm.commit()
            pending = [a for a in await client.activities() if a["status"] == "to_confirm"]
            assert len(pending) == 1
            confirmed = await client.confirm_activity(pending[0]["id"], {"duration_minutes": 50})
            assert confirmed["status"] == "active" and confirmed["duration_minutes"] == 50
            with pytest.raises(api_module.TempoConflictError):
                await client.confirm_activity(pending[0]["id"], {"duration_minutes": 50})
            with pytest.raises(api_module.TempoNotFoundError):
                await client.decide_proposal("00000000-0000-0000-0000-000000000000", True)
            with pytest.raises(api_module.TempoConflictError):
                await client.delete_place("home")
            await client.delete_place("club")

    asyncio.run(scenario())
