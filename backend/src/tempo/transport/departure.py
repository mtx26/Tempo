"""Departure re-check from the user's CURRENT position (HA phone tracker) with live routing.

Used shortly before a mandatory trip (worker): "can I still make it, and when must I leave?".
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

from tempo.transport.itinerary import Objective, TripQuery, find_trips, journey_notes
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
        best = (
            find_trips(
                provider,
                TripQuery(
                    here,
                    target.place.coordinates,
                    depart_after=now_u,
                    arrive_before=deadline,
                    objective=Objective.LATEST_DEPARTURE,
                ),
            ).best
            if deadline > now_u
            else None
        )
        if best is not None:
            leave_at = _u(best.departure)
            if planned_departure is not None and leave_at < _u(planned_departure):
                advice = DepartureStatus.LEAVE_EARLIER
            elif leave_at - now_u <= LEAVE_NOW_WINDOW:
                advice = DepartureStatus.LEAVE_NOW
            else:
                advice = DepartureStatus.ON_TIME
            return DepartureAdvice(advice, best, leave_at, warnings=journey_notes(best).all)

        earliest = find_trips(
            provider,
            TripQuery(
                here,
                target.place.coordinates,
                depart_after=now_u,
                objective=Objective.EARLIEST_ARRIVAL,
            ),
        ).best
    except RoutingUnavailableError as exc:
        return DepartureAdvice(DepartureStatus.UNKNOWN_ROUTING, reason=str(exc))

    if earliest is None:
        return DepartureAdvice(DepartureStatus.IMPOSSIBLE, reason="no journey found from here")
    arrival = _u(earliest.arrival)
    if arrival <= start:
        return DepartureAdvice(
            DepartureStatus.AT_RISK,
            earliest,
            _u(earliest.departure),
            reason=f"arrives {int((start - arrival).total_seconds() // 60)} min before the "
            f"start, less than the {int(buffer.total_seconds() // 60)} min margin",
            warnings=journey_notes(earliest).all,
        )
    return DepartureAdvice(
        DepartureStatus.LATE,
        earliest,
        _u(earliest.departure),
        minutes_late=-(-int((arrival - start).total_seconds()) // 60),
        reason="earliest possible arrival is after the start",
        warnings=journey_notes(earliest).all,
    )
