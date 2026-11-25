"""Where the user is, in one place (ARCHITECTURE.md §7, D07).

- Current position: ONLY the Home Assistant phone tracker, trusted when fresh and accurate
  (``assess_position``); used for immediate trips (journey search, departure monitoring).
- Future trips: an outing starts from the base at its start and returns to the base at its
  end (``base_at``: planned location covering that instant, else the residence of the
  alternation in force, else home); between commitments the previous commitment's place is the
  origin (``travel``): no return home is assumed between activities.
- Residence alternation: residences rotate every ``period`` at the same local time (e.g. every
  Friday at noon: the morning belongs to the previous residence, the evening to the next one).
  Planned locations are temporary exceptions: they never shift the recurrence.
- An explicit origin given in the conversation is a known place.
Ambiguity is reported, never guessed.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from tempo.timeutil import utc
from tempo.transport.places import Coordinates, Place

MAX_POSITION_AGE = timedelta(minutes=10)
MAX_POSITION_ACCURACY_M = 200.0


class PositionStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    INACCURATE = "inaccurate"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class PhonePosition:
    coordinates: Coordinates
    accuracy_m: float
    observed_at: datetime
    entity_id: str

    def __post_init__(self) -> None:
        utc(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class LocatedEvent:
    start: datetime
    end: datetime
    place: Place

    def __post_init__(self) -> None:
        if utc(self.end, "end") <= utc(self.start, "start"):
            raise ValueError("event end must be after start")


@dataclass(frozen=True, slots=True)
class DeclaredStay:
    """User statement such as "from Friday evening to Sunday I am at <place>"."""

    place: Place
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if utc(self.end, "end") <= utc(self.start, "start"):
            raise ValueError("stay end must be after start")


@dataclass(frozen=True, slots=True)
class Alternation:
    """Residences in rotation order; ``residences[0]`` starts at ``first_day`` ``switch_at``
    (local time), then the next one every ``period_days`` days at the same local time."""

    residences: tuple[Place, ...]
    first_day: date
    switch_at: time
    period_days: int
    timezone: ZoneInfo

    def __post_init__(self) -> None:
        if not self.residences or self.period_days < 1:
            raise ValueError("an alternation needs residences and a positive period")

    def switch_moment(self, index: int) -> datetime:
        """Local instant of the ``index``-th change (0 = the declared one)."""
        day = self.first_day + timedelta(days=index * self.period_days)
        return datetime.combine(day, self.switch_at, tzinfo=self.timezone)

    def index_at(self, at: datetime) -> int:
        """Number of changes since the declared one (negative before it)."""
        local = utc(at, "at").astimezone(self.timezone)
        index = (local.date() - self.first_day).days // self.period_days
        if local < self.switch_moment(index):
            index -= 1
        return index

    def residence_at(self, at: datetime) -> Place:
        return self.residences[self.index_at(at) % len(self.residences)]


def assess_position(
    position: PhonePosition | None,
    now: datetime,
    *,
    max_age: timedelta = MAX_POSITION_AGE,
    max_accuracy_m: float = MAX_POSITION_ACCURACY_M,
) -> PositionStatus:
    if position is None:
        return PositionStatus.UNAVAILABLE
    age = utc(now, "now") - position.observed_at.astimezone(UTC)
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
    instant = utc(at, "at")
    covering = [s for s in stays if utc(s.start, "start") <= instant < utc(s.end, "end")]
    if len(covering) > 1:
        raise AmbiguousLocationError
    return covering[0].place if covering else None


def base_at(
    at: datetime,
    stays: list[DeclaredStay],
    home: Place,
    alternation: Alternation | None = None,
) -> Place | None:
    """Where the user is based at instant ``at``: the planned location covering it (temporary
    exception), else the residence of the alternation, else home.

    Planned locations may change during a day ("I leave from dad's and go back to mum's after
    class"): each trip starts from the base at its departure and returns to the base at its
    arrival. Two overlapping planned locations are ambiguous: None (Tempo asks, never guesses;
    no automatic switching between homes)."""
    try:
        planned = planned_place(at, stays)
    except AmbiguousLocationError:
        return None
    if planned is not None:
        return planned
    return alternation.residence_at(at) if alternation is not None else home
