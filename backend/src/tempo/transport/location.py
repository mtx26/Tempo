"""Where the user is, in one place (ARCHITECTURE.md §7, D07).

- Current position: ONLY the Home Assistant phone tracker, trusted when fresh and accurate
  (``assess_position``); used for immediate trips (journey search, departure monitoring).
- Future presence: an outing starts from the base at its start and returns to the base at its
  end (``base_at``: planned location covering that instant, else the residence of the
  alternation in force); between commitments the user goes directly from one to the next: no
  return to a residence is ever assumed (only an explicit planned location in between).
- Residence alternation: residences rotate every ``period_days`` days; the change happens
  after the last class of the change day (the user leaves from the current residence, attends
  university and goes to the next one). Without classes that day and without a time stated by
  the user, the moment is unknown: Tempo asks, never guesses. Planned locations are temporary
  exceptions: they never shift the recurrence.
- An explicit origin given in the conversation is a known place.
Ambiguity is reported, never guessed.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from tempo.const import MAX_POSITION_ACCURACY_M, MAX_POSITION_AGE
from tempo.timeutil import utc
from tempo.transport.places import Coordinates, Place


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
    """Where the user must be, and when (a class with tolerance: the attended part)."""

    start: datetime
    end: datetime
    place: Place | None
    """None: the location text is unknown (asked, never guessed)."""
    ref: str = ""
    title: str = ""
    official_start: datetime | None = None
    """The official start shown to the user (the attended part may begin later)."""
    official_end: datetime | None = None
    """The official end (a residence change happens after the last class officially ends)."""

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
    """Residences in rotation order; ``residences[0]`` is in force from the change of
    ``first_day``, then the next one every ``period_days`` days.

    The change moment of a change day is, in order: a time the user stated for that date
    (``switches``), the time the user stated for every change (``switch_at``), else the end of
    the last class that day (``switches`` too, resolved from the timetable by the caller).
    Otherwise it is unknown and the whole change day has no known residence."""

    residences: tuple[Place, ...]
    first_day: date
    period_days: int
    timezone: ZoneInfo
    switch_at: time | None = None
    switches: dict[date, datetime] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.residences or self.period_days < 1:
            raise ValueError("an alternation needs residences and a positive period")

    def change_day(self, index: int) -> date:
        return self.first_day + timedelta(days=index * self.period_days)

    def switch_moment(self, index: int) -> datetime | None:
        """Instant of the ``index``-th change (0 = the declared one); None when unknown."""
        day = self.change_day(index)
        if day in self.switches:
            return self.switches[day]
        if self.switch_at is not None:
            return datetime.combine(day, self.switch_at, tzinfo=self.timezone)
        return None

    def index_at(self, at: datetime) -> int | None:
        """Number of changes since the declared one (negative before it); None when ``at``
        falls on a change day whose moment is unknown."""
        local = utc(at, "at").astimezone(self.timezone)
        index = (local.date() - self.first_day).days // self.period_days
        if local.date() != self.change_day(index):
            return index
        moment = self.switch_moment(index)
        if moment is None:
            return None
        return index - 1 if utc(at) < utc(moment) else index

    def residence_at(self, at: datetime) -> Place | None:
        if len(self.residences) == 1:
            return self.residences[0]  # one residence: no change ever
        index = self.index_at(at)
        return None if index is None else self.residences[index % len(self.residences)]

    def unknown_days(self, start: datetime, end: datetime) -> list[date]:
        """Change days in ``[start, end)`` whose moment is unknown (to ask the user)."""
        if len(self.residences) == 1:
            return []
        first = utc(start).astimezone(self.timezone).date()
        last = utc(end).astimezone(self.timezone).date()
        index = -(-(first - self.first_day).days // self.period_days)
        found: list[date] = []
        while (day := self.change_day(index)) <= last:
            if self.switch_moment(index) is None:
                found.append(day)
            index += 1
        return found


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


def base_at(at: datetime, stays: list[DeclaredStay], alternation: Alternation) -> Place | None:
    """Where the user is based at instant ``at``: the planned location covering it (temporary
    exception), else the residence of the alternation.

    The base may change during a day ("I leave from dad's and go back to mum's after class"):
    each outing starts from the base at its departure and returns to the base at its arrival.
    None when unknown: two overlapping planned locations, or a change day without known
    moment (Tempo asks, never guesses)."""
    try:
        planned = planned_place(at, stays)
    except AmbiguousLocationError:
        return None
    if planned is not None:
        return planned
    return alternation.residence_at(at)
