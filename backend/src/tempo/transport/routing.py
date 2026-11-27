"""Routing abstraction: journeys, legs and the provider protocol.

Providers must raise ``RoutingUnavailableError`` rather than return partial or invented journeys.
"""

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from tempo.timeutil import utc
from tempo.transport.places import Coordinates


class RoutingUnavailableError(RuntimeError):
    """No trustworthy routing answer (service down, timeout, malformed response...)."""


STREET_MODES = frozenset(
    {
        "WALK",
        "BIKE",
        "RENTAL",
        "CAR",
        "HGV",
        "CAR_PARKING",
        "CAR_DROPOFF",
        "ODM",
        "RIDE_SHARING",
        "ACCESS",  # fixed user-given access to transport (``transport.access``)
    }
)


SEVERE_EFFECTS = frozenset(
    {"NO_SERVICE", "REDUCED_SERVICE", "SIGNIFICANT_DELAYS", "DETOUR", "STOP_MOVED"}
)


def _words(text: str) -> frozenset[str]:
    """Lower-case words without accents (alert relevance)."""
    plain = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c if c.isalnum() else " " for c in plain if not unicodedata.combining(c))
    return frozenset(plain.split())


@dataclass(frozen=True, slots=True)
class ServiceAlert:
    """Operator notice attached to a leg by MOTIS (GTFS-RT Service Alerts).

    MOTIS attaches network-wide notices (e.g. every SNCB alert on every SNCB leg) and notices
    outside their impact period, so an alert is NOT proof that the leg is affected: Tempo
    reports alerts relevant in time as warnings and never drops a journey because of them.
    Cancellations and delays come from trip updates (``Leg.cancelled``, ``Leg.delay``).
    """

    header: str
    description: str = ""
    cause: str = "UNKNOWN_CAUSE"
    effect: str = "UNKNOWN_EFFECT"
    url: str | None = None
    impact_periods: tuple[tuple[datetime | None, datetime | None], ...] = ()
    """Empty: no period published (unbounded)."""

    @property
    def severe(self) -> bool:
        return self.effect in SEVERE_EFFECTS

    def active_during(self, start: datetime, end: datetime) -> bool:
        if not self.impact_periods:
            return True
        s, e = utc(start, "start"), utc(end, "end")
        return any(
            (p_start is None or utc(p_start, "period start") < e)
            and (p_end is None or utc(p_end, "period end") > s)
            for p_start, p_end in self.impact_periods
        )


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
    alerts: tuple[ServiceAlert, ...] = ()
    departure_track: str | None = None
    arrival_track: str | None = None
    scheduled_departure_track: str | None = None
    scheduled_arrival_track: str | None = None
    departure_time_reason: str | None = None
    arrival_time_reason: str | None = None
    skipped_stops: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        start, end = utc(self.start, "leg start"), utc(self.end, "leg end")
        utc(self.scheduled_start, "leg scheduled start")
        utc(self.scheduled_end, "leg scheduled end")
        if end < start:
            raise ValueError("leg ends before it starts")

    @property
    def is_transit(self) -> bool:
        return self.mode not in STREET_MODES

    @property
    def delay(self) -> timedelta:
        return self.start.astimezone(UTC) - self.scheduled_start.astimezone(UTC)

    @property
    def arrival_delay(self) -> timedelta:
        return self.end.astimezone(UTC) - self.scheduled_end.astimezone(UTC)

    @property
    def relevant_alerts(self) -> tuple[ServiceAlert, ...]:
        """Alerts about THIS leg: impact period overlapping it (scheduled or live times) and
        text naming its line or one of its stops. MOTIS attaches network-wide notices to every
        leg of an operator; those name other lines or places and are left out."""
        first = min(utc(self.start, "s"), utc(self.scheduled_start, "s"))
        last = max(utc(self.end, "e"), utc(self.scheduled_end, "e"))
        return tuple(a for a in self.alerts if a.active_during(first, last) and self._named_in(a))

    def _named_in(self, alert: ServiceAlert) -> bool:
        words = _words(f"{alert.header} {alert.description}")
        line = _words(self.line or "")
        stops = [_words(name) for name in (self.origin_name, self.destination_name)]
        return bool(line and line <= words) or any(
            stop and stop <= words for stop in stops if sum(len(w) for w in stop) >= 4
        )


@dataclass(frozen=True, slots=True)
class Journey:
    legs: tuple[Leg, ...]
    departure: datetime
    arrival: datetime
    transfers: int
    source: str

    def __post_init__(self) -> None:
        """Reject inconsistent itineraries: they could hide a mandatory trip."""
        departure = utc(self.departure, "departure")
        arrival = utc(self.arrival, "arrival")
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
    def duration(self) -> timedelta:
        return self.arrival.astimezone(UTC) - self.departure.astimezone(UTC)

    @property
    def uses_realtime(self) -> bool:
        return any(leg.realtime for leg in self.legs)

    @property
    def disruptions(self) -> tuple[ServiceAlert, ...]:
        """Distinct alerts relevant in time to at least one leg (network-wide ones included)."""
        seen: dict[ServiceAlert, None] = {}
        for leg in self.legs:
            for alert in leg.relevant_alerts:
                seen.setdefault(alert, None)  # frozen dataclass: all fields form the key
        return tuple(seen)

    @property
    def timed_disruptions(self) -> tuple[ServiceAlert, ...]:
        """Relevant alerts with a published impact period overlapping the journey.

        Alerts without any period (all SNCB notices observed on 2026-11-23, which MOTIS attaches
        to every SNCB leg) cannot be tied to this journey and are excluded here."""
        return tuple(alert for alert in self.disruptions if alert.impact_periods)

    @property
    def untimed_disruptions(self) -> tuple[ServiceAlert, ...]:
        return tuple(alert for alert in self.disruptions if not alert.impact_periods)

    @property
    def severe_disruptions(self) -> int:
        return sum(1 for alert in self.timed_disruptions if alert.severe)


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
