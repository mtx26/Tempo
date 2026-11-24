"""Where the user is, in one place (ARCHITECTURE.md §7, D07).

- Current position: ONLY the Home Assistant phone tracker, trusted when fresh and accurate
  (``assess_position``); used for immediate trips (journey search, departure monitoring).
- Future trips: each day starts and ends at its base (``day_base``: planned location, else
  home); between commitments the previous commitment's place is the origin (``travel``).
- An explicit origin given in the conversation is a known place.
Ambiguity is reported, never guessed.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from tempo.transport.places import Coordinates, Place

MAX_POSITION_AGE = timedelta(minutes=10)
MAX_POSITION_ACCURACY_M = 200.0


class PositionStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    INACCURATE = "inaccurate"
    UNAVAILABLE = "unavailable"


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


class AmbiguousLocationError(Exception):
    """Several planned locations cover the instant: Tempo asks instead of choosing."""


def planned_place(at: datetime, stays: list[DeclaredStay]) -> Place | None:
    """The planned location covering ``at`` (None if none).

    Raises ``AmbiguousLocationError`` when several planned locations overlap."""
    instant = _utc("at", at)
    covering = [s for s in stays if _utc("start", s.start) <= instant < _utc("end", s.end)]
    if len(covering) > 1:
        raise AmbiguousLocationError
    return covering[0].place if covering else None


def day_base(day: date, stays: list[DeclaredStay], home: Place, tz: ZoneInfo) -> Place | None:
    """Where the user starts and ends ``day``: the planned location covering its noon, else
    home. Two overlapping planned locations are ambiguous: None (Tempo asks, never guesses;
    no automatic switching between homes)."""
    try:
        planned = planned_place(datetime.combine(day, time(12), tzinfo=tz), stays)
    except AmbiguousLocationError:
        return None
    return planned or home
