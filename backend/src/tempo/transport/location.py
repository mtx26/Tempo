"""Current position (HA phone tracker) and origin inference for future trips.

- The phone tracker is the only source of the *current* position; it is checked for freshness
  and accuracy before use.
- A future trip starts from the preceding event of the same local day, or from a place the user
  declared for that period. Today's GPS is used only for near-term departures.
- Otherwise the result is NEEDS_CLARIFICATION: Tempo asks instead of guessing (no automatic
  switching between family homes).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from tempo.transport.places import Coordinates, Place

DEFAULT_TIMEZONE = ZoneInfo("Europe/Brussels")
MAX_POSITION_AGE = timedelta(minutes=10)
MAX_POSITION_ACCURACY_M = 200.0
NEAR_TERM = timedelta(hours=2)


class PositionStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    INACCURATE = "inaccurate"
    UNAVAILABLE = "unavailable"


class OriginSource(StrEnum):
    PREVIOUS_EVENT = "previous_event"
    CURRENT_POSITION = "current_position"
    DECLARED_STAY = "declared_stay"
    NEEDS_CLARIFICATION = "needs_clarification"


def _utc(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PhonePosition:
    coordinates: Coordinates
    accuracy_m: float
    observed_at: datetime
    entity_id: str

    def __post_init__(self) -> None:
        _utc("observed_at", self.observed_at)


@dataclass(frozen=True, slots=True)
class LocatedEvent:
    start: datetime
    end: datetime
    place: Place

    def __post_init__(self) -> None:
        if _utc("end", self.end) <= _utc("start", self.start):
            raise ValueError("event end must be after start")


@dataclass(frozen=True, slots=True)
class DeclaredStay:
    """User statement such as "from Friday evening to Sunday I am at <place>"."""

    place: Place
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if _utc("end", self.end) <= _utc("start", self.start):
            raise ValueError("stay end must be after start")


@dataclass(frozen=True, slots=True)
class Origin:
    source: OriginSource
    coordinates: Coordinates | None = None
    place: Place | None = None
    reason: str = ""


def assess_position(
    position: PhonePosition | None,
    now: datetime,
    *,
    max_age: timedelta = MAX_POSITION_AGE,
    max_accuracy_m: float = MAX_POSITION_ACCURACY_M,
) -> PositionStatus:
    if position is None:
        return PositionStatus.UNAVAILABLE
    age = _utc("now", now) - position.observed_at.astimezone(UTC)
    if age < timedelta(0) or age > max_age:
        return PositionStatus.STALE
    if position.accuracy_m > max_accuracy_m:
        return PositionStatus.INACCURATE
    return PositionStatus.FRESH


def infer_origin(
    *,
    departure: datetime,
    chain: list[LocatedEvent],
    position: PhonePosition | None,
    stays: list[DeclaredStay],
    now: datetime,
    timezone: ZoneInfo = DEFAULT_TIMEZONE,
) -> Origin:
    """Where a trip departing at ``departure`` starts, or why Tempo must ask.

    Priority: fresh phone position for an imminent departure; otherwise the most recent known
    fact before departure — the preceding event of the same local day (as of its end) or a
    declared stay covering the departure (as of its start). Overlapping stays are ambiguous.
    """
    dep, now_u = _utc("departure", departure), _utc("now", now)
    if dep < now_u:
        return Origin(OriginSource.NEEDS_CLARIFICATION, reason="departure is in the past")

    reason = "no preceding event or declared stay for this future departure"
    if dep - now_u <= NEAR_TERM:
        status = assess_position(position, now)
        if status is PositionStatus.FRESH and position is not None:
            return Origin(OriginSource.CURRENT_POSITION, position.coordinates)
        reason = f"phone position is {status.value} for an imminent departure"

    covering = [s for s in stays if s.start.astimezone(UTC) <= dep < s.end.astimezone(UTC)]
    if len(covering) > 1:
        return Origin(OriginSource.NEEDS_CLARIFICATION, reason="overlapping declared stays")

    local_day = departure.astimezone(timezone).date()
    previous = [
        e
        for e in chain
        if e.end.astimezone(UTC) <= dep and e.end.astimezone(timezone).date() == local_day
    ]
    last_event = max(previous, key=lambda e: e.end.astimezone(UTC)) if previous else None
    stay = covering[0] if covering else None

    if last_event is not None and (
        stay is None or last_event.end.astimezone(UTC) >= stay.start.astimezone(UTC)
    ):
        return Origin(OriginSource.PREVIOUS_EVENT, last_event.place.coordinates, last_event.place)
    if stay is not None:
        return Origin(OriginSource.DECLARED_STAY, stay.place.coordinates, stay.place)
    return Origin(OriginSource.NEEDS_CLARIFICATION, reason=reason)
