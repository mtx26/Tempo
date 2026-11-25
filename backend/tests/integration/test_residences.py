"""Residences, alternation and transport access points through the operation catalogue
(what the AI uses in conversation), on a real PostgreSQL. Generic test data only."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from tempo.assistant.catalog import OpContext, run_operation
from tempo.db.store import access_routing
from tempo.planning.service import build_input, replan
from tests.integration.test_planning_service import NOW, TZ, seed
from tests.test_engine import FakeRouting

pytestmark = pytest.mark.integration


def op(session: Session, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant", routing=FakeRouting())
    return run_operation(ctx, name, arguments or {})


def place(session: Session, place_id: str, lat: float, lon: float, **extra: Any) -> dict[str, Any]:
    return op(
        session,
        "upsert_place",
        {
            "place_id": place_id,
            "name": place_id.title(),
            "latitude": lat,
            "longitude": lon,
            **extra,
        },
    )


def test_access_point_needs_a_known_place_and_a_stated_duration(session: Session) -> None:
    seed(session)
    place(session, "house_b", 50.30, 4.20)
    missing = place(session, "house_b", 50.30, 4.20, access_place_id="stop_b")
    assert not missing["ok"] and "inconnu" in missing["error"]
    place(session, "stop_b", 50.32, 4.18)
    no_duration = place(session, "house_b", 50.30, 4.20, access_place_id="stop_b")
    assert not no_duration["ok"] and "jamais devinée" in no_duration["error"]
    itself = place(session, "house_b", 50.30, 4.20, access_place_id="house_b", access_minutes=5)
    assert not itself["ok"]
    ok = place(session, "house_b", 50.30, 4.20, access_place_id="stop_b", access_minutes=12)
    assert ok["ok"], ok
    listed = {p["id"]: p for p in op(session, "list_places")["result"]["places"]}
    assert (listed["house_b"]["access_place_id"], listed["house_b"]["access_minutes"]) == (
        "stop_b",
        12,
    )
    kept = place(session, "house_b", 50.30, 4.20)  # omitted = unchanged
    assert kept["ok"] and session.get_bind() is not None
    blocked = op(session, "delete_place", {"place_id": "stop_b"})
    assert not blocked["ok"] and "point d'accès" in blocked["error"]
    cleared = place(session, "house_b", 50.30, 4.20, access_place_id="")
    assert cleared["ok"]
    assert op(session, "delete_place", {"place_id": "stop_b"})["ok"]


def test_access_routing_reads_places_and_the_journey_limit(session: Session) -> None:
    seed(session)
    place(session, "stop_b", 50.32, 4.18)
    place(session, "house_b", 50.30, 4.20, access_place_id="stop_b", access_minutes=12)
    assert op(session, "update_settings", {"max_journey_minutes": 150})["ok"]
    wrapped = access_routing(session, FakeRouting())
    assert wrapped._max == timedelta(minutes=150)
    assert [(a.place.id, a.point.id, a.duration) for a in wrapped._accesses] == [
        ("house_b", "stop_b", timedelta(minutes=12))
    ]


def test_one_dated_statement_sets_the_weekly_alternation(session: Session) -> None:
    seed(session)  # NOW = Monday 07/12/2026 06:00 local
    place(session, "house_b", 50.30, 4.20)
    result = op(
        session,
        "set_residence_alternation",
        {"residences": ["house_b", "home"], "from_date": "2026-12-04", "switch_at": "12:00"},
    )
    assert result["ok"], result
    periods = result["result"]["periods"]
    assert [p["place_id"] for p in periods] == ["house_b", "home", "house_b", "home"]
    assert periods[0]["end"].startswith("2026-12-11T12:00")  # next Friday noon
    where = op(session, "get_location", {"at": "2026-12-09T20:00"})["result"]
    assert (where["source"], where["place_id"]) == ("residence_alternation", "house_b")
    friday_evening = op(session, "get_location", {"at": "2026-12-11T20:00"})["result"]
    assert friday_evening["place_id"] == "home"
    # One-off exception: it wins for its night, the recurrence is not shifted.
    exception = op(
        session,
        "set_planned_location",
        {"place_id": "home", "start": "2026-12-09T18:00", "end": "2026-12-10T08:00"},
    )
    assert exception["ok"]
    assert op(session, "get_location", {"at": "2026-12-09T20:00"})["result"]["place_id"] == "home"
    assert (
        op(session, "get_location", {"at": "2026-12-10T20:00"})["result"]["place_id"] == "house_b"
    )
    schedule = op(session, "get_residence_schedule", {"periods": 2})["result"]
    assert schedule["periods"][0]["exceptions"][0]["place_id"] == "home"
    # The planner uses the same schedule.
    loaded = build_input(session, NOW, TZ)
    assert loaded.data.alternation is not None
    assert loaded.data.alternation.residence_at(NOW + timedelta(days=2)).id == "house_b"
    assert replan(session, FakeRouting(), NOW, TZ, reason="test").state == "active"
    # Permanent change = a new statement; then removal.
    assert op(
        session,
        "set_residence_alternation",
        {"residences": ["home", "house_b"], "from_date": "2026-12-04"},
    )["ok"]
    assert op(session, "get_location", {"at": "2026-12-10T20:00"})["result"]["place_id"] == "home"
    assert op(session, "clear_residence_alternation")["ok"]
    assert op(session, "get_residence_schedule")["result"] == {"residence_alternation": None}


def test_alternation_refuses_unknown_or_repeated_residences(session: Session) -> None:
    seed(session)
    unknown = op(
        session, "set_residence_alternation", {"residences": ["nowhere"], "from_date": "2026-12-04"}
    )
    assert not unknown["ok"] and "inconnu" in unknown["error"]
    twice = op(
        session,
        "set_residence_alternation",
        {"residences": ["home", "home"], "from_date": "2026-12-04"},
    )
    assert not twice["ok"]


class Geocoding(FakeRouting):
    def __init__(self, matches: list[Any]) -> None:
        super().__init__()
        self.matches = matches
        self.queries: list[tuple[str, Any]] = []

    def geocode(self, text: str, *, near: Any = None) -> list[Any]:
        self.queries.append((text, near))
        return self.matches


def test_find_address_returns_matches_to_confirm(session: Session) -> None:
    from tempo.transport.motis import AddressMatch
    from tempo.transport.places import Coordinates

    seed(session)
    found = AddressMatch("Rue X 1, 6500 Ville", Coordinates(50.2, 4.3), "ADDRESS")
    routing = Geocoding([found])
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant", routing=routing)
    result = run_operation(ctx, "find_address", {"text": "rue x 1 ville"})["result"]
    assert result["status"] == "ok"
    assert result["matches"] == [
        {"label": "Rue X 1, 6500 Ville", "kind": "ADDRESS", "latitude": 50.2, "longitude": 4.3}
    ]
    assert routing.queries[0][1] is not None  # biased toward the home place
    nothing = OpContext(session=session, now=NOW, tz=TZ, actor="assistant", routing=Geocoding([]))
    assert run_operation(nothing, "find_address", {"text": "nulle part"})["result"]["status"] == (
        "not_found"
    )
    plain = OpContext(session=session, now=NOW, tz=TZ, actor="assistant", routing=FakeRouting())
    assert run_operation(plain, "find_address", {"text": "rue x 1"})["result"]["status"] == (
        "unavailable"
    )
