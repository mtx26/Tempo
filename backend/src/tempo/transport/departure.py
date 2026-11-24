"""Departure re-check from the user's CURRENT position (HA phone tracker) with live routing.

Used shortly before a mandatory trip (worker, M7): "can I still make it, and when must I leave?".
Rules (ARCHITECTURE.md "Departure re-check"):
- Only a FRESH and accurate phone position is used; otherwise UNKNOWN_POSITION (never stale GPS,
  never a guessed home).
- The answer comes from the routing provider at check time, so MOTIS live delays and
  cancellations are included; no journey is invented. Routing outage ⇒ UNKNOWN_ROUTING.
- Target: arrive ``buffer`` before the event. If no journey keeps the buffer, the earliest arrival
  still before the event start ⇒ AT_RISK; arriving after the start ⇒ LATE (with minutes); no
  journey at all ⇒ IMPOSSIBLE. Each status is reported, never hidden.
- Within ``ARRIVED_RADIUS_M`` of the event place ⇒ ALREADY_THERE (no trip needed).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from tempo.transport.location import (
    LocatedEvent,
    PhonePosition,
    PositionStatus,
    assess_position,
)
from tempo.transport.routing import Journey, RoutingProvider, RoutingUnavailableError

DEFAULT_BUFFER = timedelta(minutes=10)
LEAVE_NOW_WINDOW = timedelta(minutes=5)
ARRIVED_RADIUS_M = 150.0
REPORTED_DELAY = timedelta(minutes=2)


class DepartureStatus(StrEnum):
    ALREADY_THERE = "already_there"
    ON_TIME = "on_time"
    """A journey keeps the buffer and leaves more than ``LEAVE_NOW_WINDOW`` from now."""
    LEAVE_NOW = "leave_now"
    LEAVE_EARLIER = "leave_earlier"
    """Like ON_TIME/LEAVE_NOW, but earlier than the previously planned departure."""
    AT_RISK = "at_risk"
    LATE = "late"
    IMPOSSIBLE = "impossible"
    UNKNOWN_POSITION = "unknown_position"
    UNKNOWN_ROUTING = "unknown_routing"


@dataclass(frozen=True, slots=True)
class DepartureAdvice:
    status: DepartureStatus
    journey: Journey | None = None
    leave_at: datetime | None = None
    minutes_late: int = 0
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


def _u(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetimes must be timezone-aware")
    return value.astimezone(UTC)


def _warnings(journey: Journey) -> list[str]:
    found: list[str] = []
    for leg in journey.legs:
        late = max(leg.delay, leg.arrival_delay)
        if leg.is_transit and leg.realtime and late >= REPORTED_DELAY:
            minutes = int(late.total_seconds() // 60)
            found.append(f"live delay +{minutes} min on {leg.line or leg.mode}")
    found.extend(
        f"disruption notice {alert.effect}: {alert.header}"
        for alert in journey.timed_disruptions
        if alert.severe
    )
    if journey.static_transit_legs:
        found.append("some legs use the static timetable only (no live data)")
    return found


def check_departure(
    target: LocatedEvent,
    position: PhonePosition | None,
    provider: RoutingProvider,
    *,
    now: datetime,
    buffer: timedelta = DEFAULT_BUFFER,
    planned_departure: datetime | None = None,
) -> DepartureAdvice:
    now_u, start = _u(now), _u(target.start)
    status = assess_position(position, now)
    if status is not PositionStatus.FRESH or position is None:
        return DepartureAdvice(
            DepartureStatus.UNKNOWN_POSITION, reason=f"phone position is {status.value}"
        )
    here = position.coordinates
    if here.distance_m(target.place.coordinates) <= ARRIVED_RADIUS_M:
        return DepartureAdvice(DepartureStatus.ALREADY_THERE)
    if start <= now_u:
        return DepartureAdvice(
            DepartureStatus.LATE,
            minutes_late=int((now_u - start).total_seconds() // 60),
            reason="the event has already started and you are not there",
        )

    deadline = start - buffer
    try:
        candidates = (
            provider.plan(here, target.place.coordinates, deadline, arrive_by=True)
            if deadline > now_u
            else []
        )
        keeping_buffer = [
            j for j in candidates if _u(j.departure) >= now_u and _u(j.arrival) <= deadline
        ]
        if keeping_buffer:
            best = max(
                keeping_buffer,
                key=lambda j: (_u(j.departure), -j.severe_disruptions, -j.transfers),
            )
            leave_at = _u(best.departure)
            if planned_departure is not None and leave_at < _u(planned_departure):
                advice = DepartureStatus.LEAVE_EARLIER
            elif leave_at - now_u <= LEAVE_NOW_WINDOW:
                advice = DepartureStatus.LEAVE_NOW
            else:
                advice = DepartureStatus.ON_TIME
            return DepartureAdvice(advice, best, leave_at, warnings=_warnings(best))

        asap = [
            j
            for j in provider.plan(here, target.place.coordinates, now_u, arrive_by=False)
            if _u(j.departure) >= now_u
        ]
    except RoutingUnavailableError as exc:
        return DepartureAdvice(DepartureStatus.UNKNOWN_ROUTING, reason=str(exc))

    if not asap:
        return DepartureAdvice(DepartureStatus.IMPOSSIBLE, reason="no journey found from here")
    earliest = min(asap, key=lambda j: (_u(j.arrival), j.severe_disruptions, j.transfers))
    arrival = _u(earliest.arrival)
    if arrival <= start:
        return DepartureAdvice(
            DepartureStatus.AT_RISK,
            earliest,
            _u(earliest.departure),
            reason=f"arrives {int((start - arrival).total_seconds() // 60)} min before the "
            f"start, less than the {int(buffer.total_seconds() // 60)} min margin",
            warnings=_warnings(earliest),
        )
    return DepartureAdvice(
        DepartureStatus.LATE,
        earliest,
        _u(earliest.departure),
        minutes_late=-(-int((arrival - start).total_seconds()) // 60),
        reason="earliest possible arrival is after the start",
        warnings=_warnings(earliest),
    )
