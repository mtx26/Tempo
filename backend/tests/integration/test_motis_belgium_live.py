"""Integration tests against a REAL nationwide MOTIS (STIB, SNCB, TEC, De Lijn + OSM Belgium).

Requires ``TEMPO_TEST_MOTIS_BE_URL`` (see routing/README.md "Nationwide"). Without it every test
is SKIPPED. Invariants only (operator coverage, deadlines, parsing), never exact times.
"""

import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from tempo.transport.motis import MotisClient
from tempo.transport.places import Coordinates

URL = os.environ.get("TEMPO_TEST_MOTIS_BE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="TEMPO_TEST_MOTIS_BE_URL not set: nationwide MOTIS not run"),
]
TZ = ZoneInfo("Europe/Brussels")
LIEGE = Coordinates(50.6245, 5.5668)
GENT = Coordinates(51.0357, 3.7106)
BASTOGNE = Coordinates(50.0003, 5.7155)
LEUVEN_STATION = Coordinates(50.8812, 4.7157)
HEVERLEE = Coordinates(50.8510, 4.7330)
LLN = Coordinates(50.6693, 4.6155)
WAVRE = Coordinates(50.7166, 4.6013)
CENTRAL = Coordinates(50.8455, 4.3570)


def tomorrow_at(hour: int) -> datetime:
    day = datetime.now(TZ).date() + timedelta(days=1)
    return datetime.combine(day, time(hour), tzinfo=TZ)


@pytest.fixture(scope="module")
def motis() -> MotisClient:
    assert URL is not None
    return MotisClient(URL, timeout_seconds=60)


@pytest.mark.parametrize(
    ("origin", "destination", "agency"),
    [
        (LIEGE, GENT, "NMBS/SNCB"),
        (BASTOGNE, LIEGE, "LETEC"),
        (LEUVEN_STATION, HEVERLEE, "De Lijn"),
        (LLN, WAVRE, "LETEC"),
    ],
)
def test_each_regional_operator_is_routed(
    motis: MotisClient, origin: Coordinates, destination: Coordinates, agency: str
) -> None:
    journeys = motis.plan(origin, destination, tomorrow_at(8), arrive_by=False)
    agencies = {leg.agency for j in journeys for leg in j.legs if leg.is_transit}
    assert agency in agencies


def test_cross_country_arrive_by_respects_the_deadline(motis: MotisClient) -> None:
    deadline = tomorrow_at(10)
    journeys = motis.plan(BASTOGNE, GENT, deadline, arrive_by=True)
    assert journeys
    assert all(j.arrival <= deadline for j in journeys)


def test_stib_legs_have_no_realtime(motis: MotisClient) -> None:
    journeys = motis.plan(CENTRAL, LLN, tomorrow_at(8), arrive_by=False)
    stib = [leg for j in journeys for leg in j.legs if leg.agency == "STIB"]
    assert all(not leg.realtime for leg in stib)


def test_alerts_are_parsed_when_realtime_alerts_are_loaded(motis: MotisClient) -> None:
    journeys = motis.plan(LIEGE, GENT, tomorrow_at(8), arrive_by=False)
    for journey in journeys:
        for alert in journey.disruptions:
            assert alert.header
