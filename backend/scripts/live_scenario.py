"""Live acceptance scenario on the dev stack (real PostgreSQL, real nationwide MOTIS, worker on).

Checks stability, minimal rescheduling, real travel, protected-change proposals approved in
conversation, partial planning with its explanation and alternatives, global simulation,
direct journeys between activities and location, through the public operation API only.
Development data only (archives every task, creates and cancels test events):

    uv run python scripts/live_scenario.py ../deploy/secrets/tempo_api_token
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx2

API = "http://127.0.0.1:18000"
TOKEN = Path(sys.argv[1]).read_text().strip()
H = {"Authorization": f"Bearer {TOKEN}"}
TZ = ZoneInfo("Europe/Brussels")
results: list[tuple[str, bool, str]] = []
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # Windows consoles
http = httpx2.Client(timeout=120)


def check(name: str, ok: bool, detail: Any = "") -> None:
    results.append((name, ok, str(detail)))
    print(("OK   " if ok else "FAIL ") + name + (f" — {str(detail)[:220]}" if detail != "" else ""))


def op(operation: str, /, **args: Any) -> Any:
    r = http.post(f"{API}/v1/operations/{operation}", json=args, headers=H)
    if r.status_code != 200:
        raise RuntimeError(f"{operation}: {r.status_code} {r.text[:300]}")
    return r.json()["result"]


def try_op(operation: str, /, **args: Any) -> tuple[int, Any]:
    r = http.post(f"{API}/v1/operations/{operation}", json=args, headers=H)
    return r.status_code, r.json()


def say(text: str) -> dict[str, Any]:
    r = http.post(
        f"{API}/v1/conversation", json={"text": text, "conversation_id": "scenario"}, headers=H
    )
    r.raise_for_status()
    return dict(r.json())


def blocks(task_id: str) -> set[tuple[str, str]]:
    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    items = op(
        "get_schedule", start=today.isoformat(), end=(today + timedelta(days=14)).isoformat()
    )["items"]
    return {
        (i["start"], i["end"]) for i in items if i["type"] == "study" and i["task_id"] == task_id
    }


day = datetime.now(TZ).date() + timedelta(days=2)
d = day.isoformat()
for place in (
    {"place_id": "home", "name": "Kot", "latitude": 50.668, "longitude": 4.612},
    {"place_id": "campus", "name": "Campus", "latitude": 50.85, "longitude": 4.35},
    {"place_id": "club", "name": "Club", "latitude": 50.7167, "longitude": 4.6111},
):
    op("upsert_place", **place)
op("set_residence_alternation", residences=["home"], from_date="2026-01-02")
op("update_settings", horizon_days=7)
op(
    "set_weekly_availability",
    windows=[{"weekday": w, "start": "08:00", "end": "20:00"} for w in range(7)],
)
for t in op("list_tasks")["tasks"]:
    op("update_task", task_id=t["id"], completed=True)  # clean slate for this scenario
task = op(
    "create_task",
    title="Scénario maths",
    estimated_minutes=600,
    splittable=True,
    target_date=(day + timedelta(days=3)).isoformat(),
)["task"]
first = op("plan_now")
check("initial plan complete", first["status"] == "complete", first.get("issues"))
before = blocks(task["id"])
check("study blocks placed", sum(1 for _ in before) > 0, len(before))

# 1. Stability: a fixed appointment in free time moves nothing.
op(
    "create_event",
    title="Dentiste",
    scheduling="fixed",
    start=f"{d}T21:00",
    duration_minutes=30,
    remote=True,
)
second = op("plan_now")
after = blocks(task["id"])
check("free-slot appointment moves no study block", before == after, second.get("diff"))

# 2. Minimal rescheduling: an appointment over ONE study block moves only what it must.
target = sorted(before)[0]
start = datetime.fromisoformat(target[0])
op(
    "create_event",
    title="Médecin",
    scheduling="fixed",
    start=start.isoformat(),
    duration_minutes=30,
    remote=True,
)
third = op("plan_now")
moved = before - blocks(task["id"])
check(
    "only the overlapped block(s) move",
    0 < len(moved) <= 2,
    {"moved": len(moved), "diff": third.get("diff")},
)

# 3. Located commitment: real MOTIS travel, the study does not overlap the trip.
op(
    "create_event",
    title="TP",
    scheduling="fixed",
    start=f"{d}T14:00",
    duration_minutes=120,
    place_id="campus",
)
fourth = op("plan_now")
items = op("get_schedule", start=f"{d}T00:00", end=f"{d}T23:59")["items"]
trips = [i for i in items if i["type"] == "travel"]
check(
    "real journeys planned around the located class",
    len(trips) >= 2 and all(t["status"] == "routed" for t in trips),
    [(t["title"], t["start"], t["status"]) for t in trips],
)
study = [i for i in items if i["type"] == "study"]
overlap = [
    (s["start"], t["start"])
    for s in study
    for t in trips
    if datetime.fromisoformat(s["start"]) < datetime.fromisoformat(t["end"])
    and datetime.fromisoformat(t["start"]) < datetime.fromisoformat(s["end"])
]
check("no study during travel", not overlap, overlap)

# 4. Protected commitment conflict -> proposal, approved in conversation.
sched = op(
    "get_schedule", start=f"{d}T00:00", end=(day + timedelta(days=3)).isoformat() + "T23:59"
)["items"]
free_block = next(i for i in sched if i["type"] == "study" and i["task_id"] == task["id"])
op("set_lock", target_id=free_block["id"], locked=True)
op("plan_now")
bs = datetime.fromisoformat(free_block["start"])
op(
    "create_event",
    title="Examen imposé",
    scheduling="fixed",
    start=bs.isoformat(),
    duration_minutes=60,
    remote=True,
)
op("plan_now")
pending = op("list_proposals")["proposals"]
check(
    "conflict with a locked block becomes a proposal",
    any(p["kind"] == "unlock_block" for p in pending),
    [p["title"] for p in pending],
)
decision = say("approuve") if len(pending) == 1 else {"reply": "plusieurs propositions"}
check("approved through the conversation", "approuvée" in decision["reply"], decision["reply"])

# 5. Partial planning: impossible strict deadline -> partial, capacity, alternatives.
op(
    "create_task",
    title="Chimie impossible",
    estimated_minutes=3000,
    splittable=True,
    target_date=(day).isoformat(),
    hard_deadline=True,
)
fifth = op("plan_now")
check(
    "impossible strict deadline is partial, the rest planned",
    fifth["status"] == "partial" and fifth["hard_deadline_misses"],
    fifth["status"],
)
explained = op("explain_plan")["active_plan"]
cap = explained.get("deadline_capacity", {}).get("Chimie impossible")
check(
    "why: needed vs free minutes before the deadline",
    cap is not None and cap["free_minutes_before_deadline"] < 3000,
    cap,
)
alts = op("propose_alternatives")
chim = next((t for t in alts.get("tasks", []) if t["title"] == "Chimie impossible"), None)
check(
    "alternatives simulated",
    chim is not None and chim["alternatives"],
    chim and [a["kind"] for a in chim["alternatives"]],
)
maths_still = blocks(task["id"])
check(
    "other work still planned despite the impossible task", len(maths_still) > 0, len(maths_still)
)

# 6. Global reorganisation only on explicit request.
code, glob = try_op("plan_now", mode="global", dry_run=True)
check(
    "global reorganisation simulated without publishing",
    code == 200 and glob["result"]["published"] is False,
    glob["result"].get("status"),
)

# 7. Journey between two activities (direct, no home detour).
acts = op("list_activities", text="TP")["activities"]
club = op(
    "create_event",
    title="Match",
    scheduling="fixed",
    start=f"{d}T18:30",
    duration_minutes=60,
    place_id="club",
)["activity"]
trip = op(
    "search_journey",
    origin=f"activity:{acts[0]['id']}",
    destination=f"activity:{club['id']}",
    objective="latest_departure",
)
check(
    "direct journey campus -> club after the class",
    trip["status"] == "ok",
    (trip.get("best") or {}).get("departure"),
)
where = op("get_location", at=f"{d}T15:00")
check("assistant knows where the user will be", where.get("place_id") == "campus", where)

# cleanup scenario data
for t in op("list_tasks")["tasks"]:
    if t["title"] in ("Scénario maths", "Chimie impossible"):
        op("update_task", task_id=t["id"], completed=True)
for a in op("list_activities")["activities"]:
    if a["title"] in ("Dentiste", "Médecin", "TP", "Examen imposé", "Match"):
        op("cancel_event", activity_id=a["id"])
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
