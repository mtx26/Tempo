"""Integration tests against a REAL running MOTIS instance.

Requires ``TEMPO_TEST_MOTIS_URL`` (e.g. http://127.0.0.1:18080, see routing/README.md) serving
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
    assert all(leg in j.static_transit_legs for j in journeys for leg in j.legs if leg in stib)


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
