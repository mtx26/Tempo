"""The single itinerary service: every journey Tempo uses is chosen here, MOTIS computes it.

Callers (conversation search, mandatory travel of the plan, flexible-activity travel estimates,
departure monitoring) describe WHAT they need in a ``TripQuery``:
- bounds: minimum departure, arrival deadline, or both (a window). With a single bound the
  search stays within ``span`` of it;
- an objective chosen by context: latest departure (outbound to an appointment: least
  waiting), earliest arrival (way back, catching up), shortest (free search, estimates);
- criteria: maximum transfers and walking.

``find_trips`` pages through the provider answers (a MOTIS answer holds only a few
itineraries), keeps the journeys that satisfy bounds and criteria, and ranks them: the best
first, then the useful alternatives. Ties are broken deterministically by severe timed
disruptions, then transfers. Nothing is fabricated: an empty result means no candidate in the
explored range; a provider failure on the first page raises ``RoutingUnavailableError`` (an
outage is never an impossibility); a failure on a later page keeps what was explored and is
reported as ``incomplete``.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from tempo.transport.places import Coordinates
from tempo.transport.routing import Journey, RoutingProvider, RoutingUnavailableError

SEARCH_SPAN = timedelta(hours=2)
"""Default window around a single bound (a conversation search)."""
MAX_PAGES = 5
STEP = timedelta(minutes=1)


class Objective(StrEnum):
    LATEST_DEPARTURE = "latest_departure"
    EARLIEST_ARRIVAL = "earliest_arrival"
    SHORTEST = "shortest"


def _u(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("itinerary bounds must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TripQuery:
    origin: Coordinates
    destination: Coordinates
    depart_after: datetime | None = None
    arrive_before: datetime | None = None
    objective: Objective = Objective.SHORTEST
    max_transfers: int | None = None
    max_walk: timedelta | None = None
    span: timedelta = SEARCH_SPAN

    def __post_init__(self) -> None:
        if self.depart_after is None and self.arrive_before is None:
            raise ValueError("a trip needs a minimum departure, an arrival deadline, or both")
        if (
            self.depart_after is not None
            and self.arrive_before is not None
            and _u(self.arrive_before) <= _u(self.depart_after)
        ):
            raise ValueError("empty time window")

    def in_bounds(self, journey: Journey) -> bool:
        departure, arrival = _u(journey.departure), _u(journey.arrival)
        if self.depart_after is not None and self.arrive_before is not None:
            return departure >= _u(self.depart_after) and arrival <= _u(self.arrive_before)
        if self.arrive_before is not None:
            deadline = _u(self.arrive_before)
            return deadline - self.span <= arrival <= deadline
        assert self.depart_after is not None  # noqa: S101 - checked in __post_init__
        earliest = _u(self.depart_after)
        return earliest <= departure <= earliest + self.span

    def meets_criteria(self, journey: Journey) -> bool:
        if self.max_transfers is not None and journey.transfers > self.max_transfers:
            return False
        return self.max_walk is None or walking(journey) <= self.max_walk


def walking(journey: Journey) -> timedelta:
    return sum(
        (leg.end - leg.start for leg in journey.legs if leg.mode == "WALK"), start=timedelta(0)
    )


RANKING: dict[Objective, Callable[[Journey], Any]] = {
    Objective.LATEST_DEPARTURE: lambda j: (
        -_u(j.departure).timestamp(),
        j.severe_disruptions,
        j.transfers,
        j.duration,
    ),
    Objective.EARLIEST_ARRIVAL: lambda j: (
        _u(j.arrival),
        j.severe_disruptions,
        j.transfers,
        j.duration,
    ),
    Objective.SHORTEST: lambda j: (j.duration, j.severe_disruptions, j.transfers, _u(j.arrival)),
}


@dataclass(slots=True)
class TripResult:
    journeys: list[Journey] = field(default_factory=list)
    """Acceptable journeys, best first."""
    explored: int = 0
    pages: int = 0
    incomplete: bool = False
    """A later page failed: the result covers only what was explored."""
    excluded_by_criteria: bool = False
    """Journeys existed within the bounds but none met the criteria."""

    @property
    def best(self) -> Journey | None:
        return self.journeys[0] if self.journeys else None

    @property
    def alternatives(self) -> list[Journey]:
        return self.journeys[1:4]


def _key(journey: Journey) -> tuple[Any, ...]:
    return (
        _u(journey.departure),
        _u(journey.arrival),
        journey.transfers,
        tuple(
            (leg.mode, leg.line, leg.origin_name, leg.destination_name, _u(leg.start), _u(leg.end))
            for leg in journey.legs
        ),
    )


def find_trips(provider: RoutingProvider, query: TripQuery) -> TripResult:
    """Explore, filter and rank journeys for ``query`` (see the module docstring).

    Direction: backward from the deadline when the objective is the latest departure or when
    only a deadline is given; forward from the minimum departure otherwise. The exploration
    stops at the first page holding an acceptable journey for latest-departure and
    earliest-arrival objectives (the provider answers in time order), and covers the whole
    window (up to ``MAX_PAGES`` requests) for the shortest one."""
    backward = query.arrive_before is not None and (
        query.depart_after is None or query.objective is Objective.LATEST_DEPARTURE
    )
    if backward:
        assert query.arrive_before is not None  # noqa: S101 - narrowing
        cursor = _u(query.arrive_before)
        stop = _u(query.depart_after) if query.depart_after is not None else cursor - query.span
    else:
        assert query.depart_after is not None  # noqa: S101 - narrowing
        cursor = _u(query.depart_after)
        stop = _u(query.arrive_before) if query.arrive_before is not None else cursor + query.span
    found: dict[tuple[Any, ...], Journey] = {}
    result = TripResult()
    while result.pages < MAX_PAGES and (cursor > stop if backward else cursor < stop):
        try:
            page = provider.plan(query.origin, query.destination, cursor, arrive_by=backward)
        except RoutingUnavailableError:
            if result.pages == 0:
                raise
            result.incomplete = True
            break
        result.pages += 1
        new = [j for j in page if _key(j) not in found]
        found.update((_key(j), j) for j in new)
        if query.objective is not Objective.SHORTEST and any(
            query.in_bounds(j) and query.meets_criteria(j) for j in new
        ):
            break
        if backward:
            earlier = [_u(j.arrival) for j in page if _u(j.arrival) <= cursor]
            if not new or not earlier:
                break
            cursor = min(earlier) - STEP
        else:
            later = [_u(j.departure) for j in page if _u(j.departure) >= cursor]
            if not new or not later:
                break
            cursor = max(later) + STEP
    in_bounds = [j for j in found.values() if query.in_bounds(j)]
    acceptable = [j for j in in_bounds if query.meets_criteria(j)]
    result.explored = len(found)
    result.excluded_by_criteria = bool(in_bounds) and not acceptable
    result.journeys = sorted(acceptable, key=RANKING[query.objective])
    return result


REPORTED_DELAY = timedelta(minutes=2)
STATIC_ONLY = (
    "horaire théorique pour une partie du trajet : pas de temps réel, ponctualité non confirmée"
)


@dataclass(frozen=True, slots=True)
class JourneyNotes:
    """What the user must know about a chosen journey (informative, never blocking)."""

    specific: tuple[str, ...]
    """Live delays and severe disruptions active during this trip."""
    general: tuple[str, ...]
    """Operator notices without a date, and legs without realtime (static timetable: NOT a
    confirmation of punctuality) — the same for every journey, reported once."""

    @property
    def all(self) -> list[str]:
        return [*self.specific, *self.general]


def journey_notes(journey: Journey) -> JourneyNotes:
    specific: list[str] = []
    for leg in journey.legs:
        late = max(leg.delay, leg.arrival_delay)
        if leg.is_transit and leg.realtime and late >= REPORTED_DELAY:
            minutes = int(late.total_seconds() // 60)
            specific.append(f"retard en temps réel +{minutes} min sur {leg.line or leg.mode}")
    specific += [
        f"perturbation {alert.effect} : {alert.header}"
        for alert in journey.timed_disruptions
        if alert.severe
    ]
    general: list[str] = []
    if any(alert.severe for alert in journey.untimed_disruptions):
        agencies = sorted(
            {
                leg.agency or leg.mode
                for leg in journey.legs
                for alert in leg.alerts
                if not alert.impact_periods
            }
        )
        general.append(
            f"avis d'exploitant sans période d'impact ({', '.join(agencies)}) : non vérifiés "
            "pour les trajets prévus"
        )
    if journey.static_transit_legs:
        general.append(STATIC_ONLY)
    return JourneyNotes(tuple(dict.fromkeys(specific)), tuple(general))


class CachedRouting:
    """Provider wrapper for one planning run: identical requests reach MOTIS once."""

    def __init__(self, provider: RoutingProvider) -> None:
        self._provider = provider
        self._cache: dict[tuple[Coordinates, Coordinates, datetime, bool], list[Journey]] = {}

    def plan(
        self, origin: Coordinates, destination: Coordinates, time: datetime, *, arrive_by: bool
    ) -> list[Journey]:
        key = (origin, destination, _u(time), arrive_by)
        if key not in self._cache:
            self._cache[key] = self._provider.plan(origin, destination, time, arrive_by=arrive_by)
        return self._cache[key]
