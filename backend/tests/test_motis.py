"""MOTIS client contract tests with a mocked HTTP transport.

These tests prove the client builds requests and parses responses according to the OpenAPI
contract as read on 2026-11-23. They do NOT prove compatibility with a running MOTIS server.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from tempo.transport.motis import MotisClient
from tempo.transport.places import Coordinates
from tempo.transport.routing import RoutingUnavailableError

FIXTURE: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "motis_plan_v6.json").read_text(encoding="utf-8")
)
ORIGIN = Coordinates(50.668, 4.612)
DESTINATION = Coordinates(50.85, 4.35)
ARRIVE_BY = datetime(2026, 12, 1, 8, 30, tzinfo=UTC)


def client(handler: Any) -> MotisClient:
    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    return MotisClient("http://tempo-routing:8080", http_client=http)


def ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=FIXTURE)


def test_request_uses_v6_plan_with_arrive_by_and_coordinates() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return ok(request)

    client(handler).plan(ORIGIN, DESTINATION, ARRIVE_BY, arrive_by=True)
    (request,) = seen
    assert request.url.path == "/api/v6/plan"
    params = request.url.params
    assert params["fromPlace"] == "50.668000,4.612000"
    assert params["toPlace"] == "50.850000,4.350000"
    assert params["arriveBy"] == "true"
    assert params["time"] == "2026-12-01T08:30:00Z"


def test_itineraries_are_parsed_with_realtime_flags() -> None:
    journeys = client(ok).plan(ORIGIN, DESTINATION, ARRIVE_BY, arrive_by=True)
    transit = next(j for j in journeys if j.transfers == 1)
    assert transit.departure == datetime(2026, 12, 1, 6, 40, tzinfo=UTC)
    assert transit.arrival == datetime(2026, 12, 1, 7, 25, tzinfo=UTC)
    assert [leg.mode for leg in transit.legs] == ["WALK", "REGIONAL_RAIL", "WALK"]
    rail = transit.legs[1]
    assert rail.realtime is True
    assert rail.line == "S8"
    assert rail.agency == "SNCB"
    assert rail.delay == timedelta(minutes=2)
    assert transit.uses_realtime is True


def test_direct_walking_route_is_kept_as_a_valid_option() -> None:
    journeys = client(ok).plan(ORIGIN, DESTINATION, ARRIVE_BY, arrive_by=True)
    walk = next(j for j in journeys if all(leg.mode == "WALK" for leg in j.legs))
    assert walk.duration == timedelta(minutes=90)
    assert walk.uses_realtime is False


def test_itinerary_with_cancelled_leg_is_excluded() -> None:
    journeys = client(ok).plan(ORIGIN, DESTINATION, ARRIVE_BY, arrive_by=True)
    assert all(not leg.cancelled for j in journeys for leg in j.legs)
    assert len(journeys) == 2


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_http_errors_raise_routing_unavailable(status: int) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"error": "boom"})

    with pytest.raises(RoutingUnavailableError):
        client(handler).plan(ORIGIN, DESTINATION, ARRIVE_BY, arrive_by=True)


def test_timeout_raises_routing_unavailable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(RoutingUnavailableError):
        client(handler).plan(ORIGIN, DESTINATION, ARRIVE_BY, arrive_by=True)


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"itineraries": "nope"}', b'{"itineraries": [{"legs": []}]}'],
)
def test_malformed_response_raises_instead_of_partial_data(body: bytes) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=body)

    with pytest.raises(RoutingUnavailableError):
        client(handler).plan(ORIGIN, DESTINATION, ARRIVE_BY, arrive_by=True)


def test_naive_query_time_is_rejected() -> None:
    with pytest.raises(ValueError):
        client(ok).plan(ORIGIN, DESTINATION, datetime(2026, 12, 1, 8), arrive_by=True)


@pytest.mark.parametrize("url", ["ftp://x", "tempo-routing:8080", ""])
def test_invalid_base_url_is_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        MotisClient(url)
