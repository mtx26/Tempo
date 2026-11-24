"""End-to-end check: REAL Home Assistant + Tempo integration + dev backend (Docker).

Onboards a throw-away HA instance, adds the Tempo integration through its config flow, then
talks to Tempo through HA's conversation API and checks the backend state. Development only:
never point it at a real Home Assistant (it creates a user and a config entry).

    uv run python scripts/ha_e2e.py ../deploy/secrets/tempo_api_token

Prerequisites (README.md "Recette Home Assistant réelle"): dev stack up (api on 127.0.0.1:18000),
a fresh HA container on 127.0.0.1:18123 joined to the ``tempo-dev_front`` network with
``custom_components/tempo`` copied into its config directory.
"""

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx2

HA = "http://127.0.0.1:18123"
BACKEND = "http://127.0.0.1:18000"
TOKEN = Path(sys.argv[1]).read_text().strip()
BACK = {"Authorization": f"Bearer {TOKEN}"}
CLIENT_ID = f"{HA}/"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def wait_ha(http: httpx2.Client) -> None:
    for _ in range(180):
        try:
            if http.get(f"{HA}/api/onboarding").status_code in (200, 404):
                return
        except httpx2.HTTPError:
            pass
        time.sleep(2)
    raise SystemExit("Home Assistant did not start")


def onboard(http: httpx2.Client) -> str:
    user = http.post(
        f"{HA}/api/onboarding/users",
        json={
            "client_id": CLIENT_ID,
            "name": "E2E",
            "username": "e2e",
            "password": "e2e-throwaway-Passw0rd",
            "language": "fr",
        },
    )
    user.raise_for_status()
    token = http.post(
        f"{HA}/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": user.json()["auth_code"],
            "client_id": CLIENT_ID,
        },
    )
    token.raise_for_status()
    access = str(token.json()["access_token"])
    auth = {"Authorization": f"Bearer {access}"}
    http.post(f"{HA}/api/onboarding/core_config", headers=auth)
    http.post(f"{HA}/api/onboarding/analytics", headers=auth)
    http.post(
        f"{HA}/api/onboarding/integration",
        headers=auth,
        json={"client_id": CLIENT_ID, "redirect_uri": f"{HA}/?auth_callback=1"},
    )
    return access


def main() -> None:
    with httpx2.Client(timeout=60) as http:
        wait_ha(http)
        auth = {"Authorization": f"Bearer {onboard(http)}"}

        # Backend: places the user would have given in conversation (coordinates never guessed).
        for place in (
            {"place_id": "home", "name": "Kot", "latitude": 50.668, "longitude": 4.612},
            {"place_id": "campus", "name": "Campus", "latitude": 50.85, "longitude": 4.35},
        ):
            r = http.post(f"{BACKEND}/v1/operations/upsert_place", json=place, headers=BACK)
            r.raise_for_status()
        http.post(
            f"{BACKEND}/v1/operations/update_settings", json={"home_place_id": "home"}, headers=BACK
        ).raise_for_status()

        # Config flow exactly as the HA screens drive it.
        flow = http.post(
            f"{HA}/api/config/config_entries/flow",
            headers=auth,
            json={"handler": "tempo", "show_advanced_options": False},
        ).json()
        check("config flow opens on the user step", flow.get("step_id") == "user", str(flow)[:200])
        bad = http.post(
            f"{HA}/api/config/config_entries/flow/{flow['flow_id']}",
            headers=auth,
            json={
                "url": "http://api:8000",
                "api_token": "wrong-token-xxxxxxxxxxxx",
                "verify_ssl": True,
            },
        ).json()
        check(
            "wrong token is refused", bad.get("errors") == {"base": "invalid_auth"}, str(bad)[:200]
        )
        step = http.post(
            f"{HA}/api/config/config_entries/flow/{flow['flow_id']}",
            headers=auth,
            json={"url": "http://api:8000", "api_token": TOKEN, "verify_ssl": True},
        ).json()
        check(
            "valid token leads to the tracker step",
            step.get("step_id") == "tracker",
            str(step)[:200],
        )
        done = http.post(
            f"{HA}/api/config/config_entries/flow/{flow['flow_id']}",
            headers=auth,
            json={"tracker_entity_id": "device_tracker.phone"},
        ).json()
        check("config entry created", done.get("type") == "create_entry", str(done)[:200])
        settings = http.get(f"{BACKEND}/v1/settings", headers=BACK).json()
        check(
            "tracker pushed to the backend",
            settings.get("tracker_entity_id") == "device_tracker.phone",
        )
        time.sleep(8)

        states = http.get(f"{HA}/api/states", headers=auth).json()
        tempo = sorted(s["entity_id"] for s in states if "tempo" in s["entity_id"])
        print("entities:", tempo)
        agent = next((e for e in tempo if e.startswith("conversation.")), None)
        check("conversation agent exists", agent is not None, str(agent))
        check("calendar entity exists", any(e.startswith("calendar.") for e in tempo))
        check("todo entity exists", any(e.startswith("todo.") for e in tempo))
        services = {
            d["domain"]: d["services"] for d in http.get(f"{HA}/api/services", headers=auth).json()
        }
        business = sorted(services.get("tempo", {}))
        check("no business service registered", business == [], str(business))

        def say(text: str) -> str:
            r = http.post(
                f"{HA}/api/conversation/process",
                headers=auth,
                json={"text": text, "agent_id": agent, "language": "fr", "conversation_id": "e2e"},
            )
            r.raise_for_status()
            speech = r.json()["response"]["speech"]["plain"]["speech"]
            print(f"  > {text}\n  < {speech}")
            return str(speech)

        today = datetime.now(ZoneInfo("Europe/Brussels")).date()
        target = (today + timedelta(days=10)).strftime("%d/%m/%Y")
        added = say(f"ajoute 2h de maths pour le {target}")
        check("task added through HA conversation", "Ajouté" in added and "maths" in added, added)
        tasks = [
            t for t in http.get(f"{BACKEND}/v1/tasks", headers=BACK).json() if not t["archived"]
        ]
        check("task stored in the backend", "maths" in [t["subject"] for t in tasks], str(tasks))
        total = sum(t["remaining_minutes"] or 0 for t in tasks)
        hours, minutes = divmod(total, 60)
        expected = f"{hours} h" + (f" {minutes:02d}" if minutes else "")
        left = say("combien d'heures d'étude il me reste ?")
        check(
            "question answered from stored data", expected in left, f"{left} (attendu {expected})"
        )
        unknown = say("organise ma vie")
        check("unknown request is not invented", "pas compris" in unknown, unknown)

        # Real GPS path: the phone tracker state changes in HA -> pushed to the backend.
        http.post(
            f"{HA}/api/states/device_tracker.phone",
            headers=auth,
            json={
                "state": "home",
                "attributes": {
                    "latitude": 50.668,
                    "longitude": 4.612,
                    "gps_accuracy": 15,
                    "source_type": "gps",
                },
            },
        ).raise_for_status()
        time.sleep(3)
        position = http.post(f"{BACKEND}/v1/operations/get_position", json={}, headers=BACK).json()[
            "result"
        ]
        check(
            "phone GPS pushed by HA is a reliable origin",
            bool(position.get("reliable_now")),
            str(position)[:200],
        )

        time.sleep(35)  # one coordinator refresh
        todo = http.get(f"{HA}/api/states/todo.tempo_tasks", headers=auth).json()
        check(
            "todo list shows the remaining tasks",
            todo.get("state") == str(len(tasks)),
            str(todo)[:200],
        )

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
