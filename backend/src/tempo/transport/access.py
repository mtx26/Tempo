"""Transport access points and realistic journeys, around any routing provider (MOTIS).

A place may be reached from public transport only through another place after a fixed,
user-given duration ("from this house I am driven to that bus stop in 10 minutes"). The wrapper
routes such journeys through MOTIS from/to the access point and adds one ``ACCESS`` leg of that
exact duration, so every caller (planning, day journeys, departure checks, conversation
searches) gets complete door-to-door journeys without knowing about access points. A query
point within ``SNAP_RADIUS_M`` of such a place (e.g. the phone's GPS at home) uses it too.

Unrealistic itineraries (e.g. leaving the previous evening) are excluded with MOTIS's own
``maxTravelTime`` when the provider supports it, and filtered again here on the door-to-door
duration (access legs included). Nothing is guessed: no access duration, no access.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tempo.transport.places import Coordinates, Place
from tempo.transport.routing import Journey, Leg, RoutingProvider

ACCESS_MODE = "ACCESS"
SNAP_RADIUS_M = 300.0
"""A point this close to a place with an access point is treated as that place."""
DEFAULT_MAX_JOURNEY = timedelta(hours=3)
"""Longest acceptable door-to-door journey unless the user sets another limit."""


@dataclass(frozen=True, slots=True)
class Access:
    place: Place
    """The place the user lives at or goes to."""
    point: Place
    """Where public transport is reached from ``place``."""
    duration: timedelta


def _leg(origin: str, destination: str, start: datetime, end: datetime) -> Leg:
    return Leg(ACCESS_MODE, origin, destination, start, end, start, end, realtime=False)


class AccessRouting:
    """``RoutingProvider`` adding access legs and the journey-duration limit."""

    def __init__(
        self,
        inner: RoutingProvider,
        accesses: list[Access],
        *,
        max_journey: timedelta = DEFAULT_MAX_JOURNEY,
    ) -> None:
        self._inner = inner
        self._accesses = accesses
        self._max = max_journey

    def _access_at(self, point: Coordinates) -> Access | None:
        # min() keeps the first of equally near accesses: temporary ones are listed first.
        nearest = min(
            self._accesses,
            key=lambda a: a.place.coordinates.distance_m(point),
            default=None,
        )
        if nearest is None or nearest.place.coordinates.distance_m(point) > SNAP_RADIUS_M:
            return None
        return nearest

    def plan(
        self,
        origin: Coordinates,
        destination: Coordinates,
        time: datetime,
        *,
        arrive_by: bool,
    ) -> list[Journey]:
        start, end = self._access_at(origin), self._access_at(destination)
        if start is not None and end is not None and start.place == end.place:
            start = end = None  # a trip around one place never goes through its access point
        query_time = time
        if arrive_by and end is not None:
            query_time = time - end.duration
        elif not arrive_by and start is not None:
            query_time = time + start.duration
        extra = (start.duration if start else timedelta(0)) + (
            end.duration if end else timedelta(0)
        )
        options: dict[str, Any] = {}
        if getattr(self._inner, "accepts_max_travel", False):
            options["max_travel"] = max(self._max - extra, timedelta(minutes=1))
        journeys = self._inner.plan(
            start.point.coordinates if start else origin,
            end.point.coordinates if end else destination,
            query_time,
            arrive_by=arrive_by,
            **options,
        )
        complete = [self._door_to_door(j, start, end) for j in journeys]
        return [j for j in complete if j.duration <= self._max]

    @staticmethod
    def _door_to_door(journey: Journey, start: Access | None, end: Access | None) -> Journey:
        if start is None and end is None:
            return journey
        legs = list(journey.legs)
        departure, arrival = journey.departure, journey.arrival
        if start is not None:
            departure = journey.departure - start.duration
            legs.insert(0, _leg(start.place.name, start.point.name, departure, journey.departure))
        if end is not None:
            arrival = journey.arrival + end.duration
            legs.append(_leg(end.point.name, end.place.name, journey.arrival, arrival))
        return Journey(tuple(legs), departure, arrival, journey.transfers, journey.source)
