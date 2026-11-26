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


class Recording(FakeRouting):
    def __init__(self) -> None:
        super().__init__()
        self.origins: list[Any] = []

    def plan(self, origin: Any, destination: Any, time: Any, *, arrive_by: bool) -> Any:
        self.origins.append(origin)
        return super().plan(origin, destination, time, arrive_by=arrive_by)


def test_one_off_access_point_for_a_single_journey_is_never_saved(session: Session) -> None:
    from tempo.transport.places import Coordinates

    seed(session)  # home has no default access point
    place(session, "other_stop", 50.60, 4.55)
    routing = Recording()
    ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant", routing=routing)
    journey = {
        "origin": "place:home",
        "destination": "place:campus",
        "arrive_before": "2026-12-08T09:00",
        "access_place_id": "other_stop",
        "access_minutes": 15,
    }
    result = run_operation(ctx, "search_journey", journey)
    assert result["ok"], result
    assert routing.origins[0] == Coordinates(50.60, 4.55)  # routed from the one-off stop
    listed = {p["id"]: p for p in op(session, "list_places")["result"]["places"]}
    assert listed["home"]["access_place_id"] is None  # nothing saved
    no_duration = run_operation(ctx, "search_journey", {**journey, "access_minutes": None})
    assert not no_duration["ok"] and "inconnue" in no_duration["error"]


def test_overlapping_exceptions_keep_their_unaffected_parts(session: Session) -> None:
    seed(session)
    place(session, "house_b", 50.30, 4.20)
    op(
        session,
        "set_planned_location",
        {"place_id": "house_b", "start": "2026-12-08T00:00", "end": "2026-12-12T00:00"},
    )
    stays = op(
        session,
        "set_planned_location",
        {"place_id": "home", "start": "2026-12-09T12:00", "end": "2026-12-10T12:00"},
    )["result"]["planned_locations"]
    assert [(s["place_id"], s["start"][:16], s["end"][:16]) for s in stays] == [
        ("house_b", "2026-12-08T00:00", "2026-12-09T12:00"),
        ("home", "2026-12-09T12:00", "2026-12-10T12:00"),
        ("house_b", "2026-12-10T12:00", "2026-12-12T00:00"),
    ]
    cleared = op(
        session,
        "clear_planned_location",
        {"start": "2026-12-11T00:00", "end": "2026-12-13T00:00"},
    )["result"]["planned_locations"]
    assert cleared[-1]["end"][:16] == "2026-12-11T00:00"  # trimmed, not deleted


def test_temporary_access_serves_planned_journeys_and_keeps_the_default(session: Session) -> None:
    from tempo.transport.places import Coordinates

    seed(session)
    place(session, "stop_a", 50.66, 4.60)
    place(session, "station", 50.60, 4.55)
    place(session, "home", 50.668, 4.612, access_place_id="stop_a", access_minutes=10)
    result = op(
        session,
        "set_temporary_access",
        {
            "place_id": "home",
            "access_place_id": "station",
            "access_minutes": 15,
            "start": "2026-12-08T00:00",
            "end": "2026-12-09T00:00",
        },
    )
    assert result["ok"], result
    rules = op(session, "get_rules")["result"]
    assert rules["temporary_access"][0]["access_place_id"] == "station"
    listed = {p["id"]: p for p in op(session, "list_places")["result"]["places"]}
    assert listed["home"]["access_place_id"] == "stop_a"  # permanent default unchanged
    routing = Recording()
    wrapped = access_routing(session, routing)
    home = Coordinates(50.668, 4.612)
    campus = Coordinates(50.85, 4.35)
    wrapped.plan(home, campus, NOW + timedelta(days=1, hours=2), arrive_by=False)  # 08/12
    wrapped.plan(home, campus, NOW + timedelta(days=3), arrive_by=False)  # 10/12
    assert routing.origins == [Coordinates(50.60, 4.55), Coordinates(50.66, 4.60)]
    assert op(
        session,
        "clear_temporary_access",
        {"place_id": "home", "start": "2026-12-01T00:00", "end": "2026-12-31T00:00"},
    )["result"] == {"temporary_access": []}


def test_blocking_question_keeps_the_valid_plan(session: Session) -> None:
    from sqlalchemy import select

    from tempo.db.models import FixedEventRow, NotificationRow
    from tempo.db.store import active_version
    from tempo.domain.models import EventStatus
    from tempo.sync.ics_repository import get_or_create_source

    seed(session)
    first = replan(session, FakeRouting(), NOW, TZ, reason="test")
    assert first.state == "active"
    source = get_or_create_source(session, "université")
    session.add(
        FixedEventRow(
            source_id=source.id,
            uid="mystery",
            start_at=NOW + timedelta(days=1, hours=8),
            end_at=NOW + timedelta(days=1, hours=10),
            summary="Cours mystère",
            location="NiInconnu.42",
            status=EventStatus.CONFIRMED,
            first_seen_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()
    second = replan(session, FakeRouting(), NOW, TZ, reason="test")
    assert second.state == "rejected" and second.summary["kept_previous_plan"]
    current = active_version(session)
    assert current is not None and current.id == first.id  # the valid plan stays in force
    questions = list(
        session.scalars(select(NotificationRow).where(NotificationRow.kind == "question"))
    )
    assert questions and "NiInconnu.42" in questions[0].message
    kinds = {n.kind for n in session.scalars(select(NotificationRow))}
    assert "plan_rejected" not in kinds


def test_values_that_must_come_from_the_user_are_checked_in_conversation(
    session: Session,
) -> None:
    seed(session)
    place(session, "stop_a", 50.66, 4.60)

    def said(text: str) -> OpContext:
        ctx = OpContext(session=session, now=NOW, tz=TZ, actor="assistant", routing=FakeRouting())
        ctx.user_said = text
        return ctx

    access = {
        "place_id": "home",
        "name": "Kot",
        "latitude": 50.668,
        "longitude": 4.612,
        "access_place_id": "stop_a",
        "access_minutes": 15,
    }
    invented = run_operation(said("on me dépose à l'arrêt"), "upsert_place", access)
    assert not invented["ok"] and "jamais devinée" in invented["error"]
    assert run_operation(said("en un quart d'heure"), "upsert_place", access)["ok"]
    alternation = {"residences": ["home"], "from_date": "2026-12-04", "switch_at": "18:00"}
    guessed = run_operation(said("chaque vendredi soir"), "set_residence_alternation", alternation)
    assert not guessed["ok"] and "valeur par défaut" in guessed["error"]
    assert run_operation(said("vendredi à 18h"), "set_residence_alternation", alternation)["ok"]
    default = {**alternation, "switch_at": "12:00"}
    assert run_operation(said("chaque vendredi soir"), "set_residence_alternation", default)["ok"]
