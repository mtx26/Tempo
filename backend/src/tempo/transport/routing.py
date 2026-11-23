"""Routing abstraction: journeys, legs and the provider protocol.

Providers must raise ``RoutingUnavailableError`` rather than return partial or invented journeys.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from tempo.transport.places import Coordinates


class RoutingUnavailableError(RuntimeError):
    """No trustworthy routing answer (service down, timeout, malformed response...)."""


STREET_MODES = frozenset(
    {"WALK", "BIKE", "RENTAL", "CAR", "HGV", "CAR_PARKING", "CAR_DROPOFF", "ODM", "RIDE_SHARING"}
)


def _aware_utc(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Leg:
    mode: str
    origin_name: str
    destination_name: str
    start: datetime
    end: datetime
    scheduled_start: datetime
    scheduled_end: datetime
    realtime: bool
    """True when times include live data from the operator (GTFS-RT/SIRI via MOTIS)."""
    cancelled: bool = False
    line: str | None = None
    agency: str | None = None
    distance_m: float | None = None

    def __post_init__(self) -> None:
        start, end = _aware_utc("leg start", self.start), _aware_utc("leg end", self.end)
        _aware_utc("leg scheduled start", self.scheduled_start)
        _aware_utc("leg scheduled end", self.scheduled_end)
        if end < start:
            raise ValueError("leg ends before it starts")

    @property
    def is_transit(self) -> bool:
        return self.mode not in STREET_MODES

    @property
    def delay(self) -> timedelta:
        return self.start.astimezone(UTC) - self.scheduled_start.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Journey:
    legs: tuple[Leg, ...]
    departure: datetime
    arrival: datetime
    transfers: int
    source: str

    def __post_init__(self) -> None:
        """Reject inconsistent itineraries: they could hide a mandatory trip."""
        departure = _aware_utc("departure", self.departure)
        arrival = _aware_utc("arrival", self.arrival)
        if not self.legs:
            raise ValueError("journey without legs")
        if arrival < departure:
            raise ValueError("journey arrives before it departs")
        cursor = departure
        for leg in self.legs:
            if leg.start.astimezone(UTC) < cursor:
                raise ValueError("journey legs overlap or start before the departure")
            cursor = leg.end.astimezone(UTC)
        if cursor > arrival:
            raise ValueError("journey legs end after the arrival")

    @property
    def static_transit_legs(self) -> tuple[Leg, ...]:
        """Transit legs without live data (e.g. STIB): label them "static timetable only"."""
        return tuple(leg for leg in self.legs if leg.is_transit and not leg.realtime)

    @property
    def duration(self) -> timedelta:
        return self.arrival.astimezone(UTC) - self.departure.astimezone(UTC)

    @property
    def uses_realtime(self) -> bool:
        return any(leg.realtime for leg in self.legs)


class RoutingProvider(Protocol):
    def plan(
        self,
        origin: Coordinates,
        destination: Coordinates,
        time: datetime,
        *,
        arrive_by: bool,
    ) -> list[Journey]:
        """Journeys arriving by ``time`` (``arrive_by``) or departing from ``time``."""
        ...
