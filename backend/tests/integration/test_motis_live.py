"""Integration tests against a REAL running MOTIS instance.

Requires ``TEMPO_TEST_MOTIS_URL`` (e.g. http://127.0.0.1:18080, see README.md "MOTIS local") serving
the Brussels test region (STIB + SNCB GTFS, SNCB GTFS-RT, BBBike Brussels OSM). Without it every
test here is SKIPPED with an explicit reason.

Assertions are deliberately about invariants (arrival before the deadline, walking legs, STIB
without realtime...), not exact departure times, because real timetables change daily.
"""

import os
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx2
import pytest

from tempo.transport.location import LocatedEvent
from tempo.transport.motis import MotisClient
from tempo.transport.places import Coordinates, Place
from tempo.transport.routing import RoutingUnavailableError
from tempo.transport.travel import TravelStatus, plan_day_travel

MOTIS_URL = os.environ.get("TEMPO_TEST_MOTIS_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not MOTIS_URL, reason="TEMPO_TEST_MOTIS_URL not set: real MOTIS not tested"),
]

TZ = ZoneInfo("Europe/Brussels")
CENTRAL = Place("central", "Bruxelles-Central", Coordinates(50.8455, 4.3571))
ULB = Place("ulb", "ULB Solbosch", Coordinates(50.8126, 4.3817))
SCHAERBEEK = Place("schaerbeek", "Gare de Schaerbeek", Coordinates(50.8784, 4.3786))


def tomorrow_at(hour: int, minute: int = 0) -> datetime:
    day = datetime.now(TZ).date() + timedelta(days=1)
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


@pytest.fixture(scope="module")
def motis() -> MotisClient:
    assert MOTIS_URL is not None
    return MotisClient(MOTIS_URL, timeout_seconds=60)


def test_health_endpoint_is_up() -> None:
    assert httpx2.get(f"{MOTIS_URL}/api/v1/health", timeout=10).status_code == 200


def test_arrive_by_journeys_respect_the_deadline(motis: MotisClient) -> None:
    deadline = tomorrow_at(9)
    journeys = motis.plan(CENTRAL.coordinates, ULB.coordinates, deadline, arrive_by=True)
    assert journeys, "no journey Bruxelles-Central -> ULB on a weekday morning"
    for journey in journeys:
        assert journey.arrival.astimezone(UTC) <= deadline.astimezone(UTC)
        assert journey.legs[0].mode == "WALK"
        assert journey.legs[-1].mode == "WALK"
        assert journey.duration < timedelta(hours=2)


def test_depart_at_journeys_do_not_leave_before_the_query(motis: MotisClient) -> None:
    start = tomorrow_at(14)
    journeys = motis.plan(CENTRAL.coordinates, SCHAERBEEK.coordinates, start, arrive_by=False)
    assert journeys
    assert all(j.departure.astimezone(UTC) >= start.astimezone(UTC) for j in journeys)


def test_stib_legs_are_never_reported_as_realtime(motis: MotisClient) -> None:
    journeys = motis.plan(CENTRAL.coordinates, ULB.coordinates, tomorrow_at(9), arrive_by=True)
    stib = [leg for j in journeys for leg in j.legs if leg.agency == "STIB"]
    assert stib, "expected STIB legs in Brussels"
    assert all(not leg.realtime for leg in stib)


def test_destination_outside_the_region_yields_no_journey(motis: MotisClient) -> None:
    paris = Coordinates(48.8566, 2.3522)
    assert motis.plan(CENTRAL.coordinates, paris, tomorrow_at(9), arrive_by=True) == []


def test_query_outside_loaded_timetable_raises_with_reason(motis: MotisClient) -> None:
    far = datetime.now(UTC) + timedelta(days=400)
    with pytest.raises(RoutingUnavailableError, match="timetable"):
        motis.plan(CENTRAL.coordinates, ULB.coordinates, far, arrive_by=True)


def test_day_travel_is_routed_end_to_end_with_real_motis(motis: MotisClient) -> None:
    events = [
        LocatedEvent(tomorrow_at(9), tomorrow_at(11), ULB),
        LocatedEvent(tomorrow_at(14), tomorrow_at(16), SCHAERBEEK),
    ]
    plan = plan_day_travel(events, motis, start_place=CENTRAL, end_place=CENTRAL)
    assert [b.status for b in plan.blocks] == [TravelStatus.ROUTED] * 3
    assert plan.issues == []
    outbound = plan.blocks[0]
    assert outbound.journey is not None
    assert outbound.journey.arrival.astimezone(UTC) <= (tomorrow_at(9) - timedelta(minutes=10))


def test_assistant_journey_search_on_real_motis(session: object, motis: MotisClient) -> None:
    """B09 on the REAL MOTIS through the operation catalogue: arrive before, depart after,
    a range, and an explicit no-candidate answer for an impossible range."""
    from sqlalchemy.orm import Session

    from tempo.assistant.catalog import OpContext, run_operation
    from tempo.db.models import PlaceRow

    assert isinstance(session, Session)
    for place in (CENTRAL, ULB):
        session.add(
            PlaceRow(
                id=place.id,
                name=place.name,
                latitude=place.coordinates.lat,
                longitude=place.coordinates.lon,
            )
        )
    session.flush()
    ctx = OpContext(session=session, now=datetime.now(UTC), tz=TZ, actor="test", routing=motis)
    common = {"origin_place_id": "central", "destination_place_id": "ulb"}

    arrive = run_operation(
        ctx, "search_journey", {**common, "arrive_before": tomorrow_at(9).isoformat()}
    )
    assert arrive["ok"] and arrive["result"]["status"] == "ok", arrive
    best = arrive["result"]["best"]
    assert datetime.fromisoformat(best["arrival"]) <= tomorrow_at(9)
    assert best["legs"] and best["duration_minutes"] < 120

    depart = run_operation(
        ctx,
        "search_journey",
        {**common, "depart_after": tomorrow_at(14).isoformat(), "objective": "earliest_arrival"},
    )
    assert depart["result"]["status"] == "ok"
    assert datetime.fromisoformat(depart["result"]["best"]["departure"]) >= tomorrow_at(14)

    window = run_operation(
        ctx,
        "search_journey",
        {
            **common,
            "depart_after": tomorrow_at(8).isoformat(),
            "arrive_before": tomorrow_at(9, 30).isoformat(),
        },
    )
    assert window["result"]["status"] == "ok"
    best = window["result"]["best"]
    assert datetime.fromisoformat(best["departure"]) >= tomorrow_at(8)
    assert datetime.fromisoformat(best["arrival"]) <= tomorrow_at(9, 30)

    tight = run_operation(
        ctx,
        "search_journey",
        {
            **common,
            "depart_after": tomorrow_at(8).isoformat(),
            "arrive_before": tomorrow_at(8, 2).isoformat(),
        },
    )
    assert tight["result"]["status"] == "no_candidate"
