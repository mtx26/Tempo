"""Mandatory travel: the journeys of one outing (base -> commitments -> base).

Conventions (ARCHITECTURE.md: travel is mandatory, never invented). Journeys are chosen by
the single itinerary service (``itinerary.find_trips``):
- Outbound trips must arrive ``buffer`` before the event and leave after the previous
  commitment; objective: latest departure (least waiting). The travel block spans from the
  chosen departure up to the event start (the margin is part of the block).
- Between two events the trip is direct: never via a residence.
- Return trips leave after the last event and must arrive before ``return_by`` (the next
  outing, or a bounded evening); objective: earliest arrival.
- Alerts never exclude a journey (MOTIS attaches network-wide notices); cancelled trips are
  already removed by the provider. Alternatives are kept on each block.
- Live delays and relevant severe notices of chosen journeys are reported as ``warnings``.
- No feasible journey ⇒ IMPOSSIBLE (reported, gap blocked). Routing outage ⇒ the itinerary of
  the published plan for the same leg is kept when it still fits (STALE: not revalidated,
  refreshed as soon as MOTIS answers again); otherwise UNKNOWN, the gap conservatively blocked
  up to the longest acceptable journey (``max_journey``). No journey is ever fabricated.
"""

import itertools
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from tempo.const import BUFFER, MAX_JOURNEY, MAX_TRAVEL, TOLERANCE_PER_MINUTE
from tempo.timeutil import utc
from tempo.transport.itinerary import (
    Objective,
    TripQuery,
    TripResult,
    find_trips,
    journey_notes,
)
from tempo.transport.location import LocatedEvent
from tempo.transport.places import Place
from tempo.transport.routing import Journey, RoutingProvider, RoutingUnavailableError

type Previous = Callable[[Place, Place, datetime, bool], Journey | None]
"""Itinerary of the published plan for a leg: (origin, destination, event start for an outbound
leg or event end for a return, outbound?)."""


class TravelStatus(StrEnum):
    ROUTED = "routed"
    STALE = "stale"
    """Routing unavailable: the itinerary of the published plan is kept, not revalidated."""
    IMPOSSIBLE = "impossible"
    UNKNOWN = "unknown"

    @property
    def feasible(self) -> bool:
        return self in (TravelStatus.ROUTED, TravelStatus.STALE)


@dataclass(frozen=True, slots=True)
class TravelBlock:
    start: datetime
    end: datetime
    origin: Place
    destination: Place
    status: TravelStatus
    journey: Journey | None = None
    alternatives: tuple[Journey, ...] = ()
    """Other acceptable journeys, best first (kept for re-planning and the user)."""
    returning: bool = False
    """Way back to the base after the last commitment of an outing (no deadline to meet)."""
    serves: tuple[str, ...] = ()
    """Refs of the events it leaves from / leads to (e.g. ``activity:<id>``)."""
    event_title: str = ""
    event_start: datetime | None = None
    """Official start of the commitment reached (outbound): what the user is shown."""
    event_end: datetime | None = None
    """Official end of the commitment left (way back): identity of the leg."""


@dataclass(slots=True)
class TravelPlan:
    blocks: list[TravelBlock] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    """Live delays and disruption notices on routed journeys (informative, not blocking)."""


@dataclass(frozen=True, slots=True)
class _Rules:
    buffer: timedelta
    max_journey: timedelta
    previous: Previous | None

    def kept(
        self, origin: Place, destination: Place, at: datetime, outbound: bool
    ) -> Journey | None:
        return None if self.previous is None else self.previous(origin, destination, at, outbound)


def _trip(
    routing: RoutingProvider,
    origin: Place,
    destination: Place,
    *,
    depart_after: datetime | None,
    arrive_before: datetime | None,
    objective: Objective,
) -> TripResult:
    return find_trips(
        routing,
        TripQuery(
            origin.coordinates,
            destination.coordinates,
            depart_after=depart_after,
            arrive_before=arrive_before,
            objective=objective,
            span=MAX_TRAVEL,
        ),
    )


def _label(origin: Place, destination: Place, when: datetime) -> str:
    # UTC in labels: one stable text per journey, whatever timezone the caller used.
    return f"{origin.name} -> {destination.name} ({utc(when).isoformat()})"


def _journey_warnings(plan: TravelPlan, journey: Journey, label: str) -> None:
    notes = journey_notes(journey)
    # Only notices about this journey; untimed operator-wide notices are never reported.
    for message in (f"{note} ({label})" for note in notes.specific):
        if message not in plan.warnings:
            plan.warnings.append(message)


def _minutes(later: datetime, earlier: datetime) -> float:
    return (utc(later) - utc(earlier)).total_seconds() / 60


def _bounds(event: LocatedEvent, tolerated: datetime, official: datetime | None) -> list[datetime]:
    """Time bounds to try: the official one first, the tolerated one when it differs."""
    first = official or tolerated
    return [first] if utc(first) == utc(tolerated) else [first, tolerated]


def _outbound(
    routing: RoutingProvider,
    plan: TravelPlan,
    origin: Place,
    target: LocatedEvent,
    previous: LocatedEvent | None,
    rules: _Rules,
) -> None:
    """Journey to ``target`` (from ``previous`` when given). The attendance tolerance of a
    class is soft: journeys arriving before the official start (with the margin) and leaving
    the previous class after its official end are tried first; one using tolerance minutes is
    kept only when it saves more than those minutes cost (``TOLERANCE_PER_MINUTE`` each, on top
    of the time spent travelling and waiting); never more than the configured tolerance."""
    destination = target.place
    assert destination is not None  # noqa: S101 - unknown places are never routed
    official_start = target.official_start or target.start
    official_deadline = official_start - rules.buffer
    serves = tuple(r for r in ((previous.ref if previous else ""), target.ref) if r)

    def block(
        start: datetime,
        end: datetime,
        status: TravelStatus,
        journey: Journey | None = None,
        alternatives: tuple[Journey, ...] = (),
    ) -> None:
        plan.blocks.append(
            TravelBlock(
                start,
                end,
                origin,
                destination,
                status,
                journey,
                alternatives,
                serves=serves,
                event_title=target.title,
                event_start=official_start,
            )
        )

    deadline = target.start - rules.buffer
    not_before = None if previous is None else previous.end
    label = _label(origin, destination, official_deadline)
    # Conservative start when nothing is known: never more than an acceptable journey.
    worst = deadline - rules.max_journey
    gap_start = worst if not_before is None else max(not_before, worst, key=utc)
    if not_before is not None and utc(not_before) >= utc(deadline):
        plan.issues.append(f"impossible connection {label}")
        block(not_before, target.start, TravelStatus.IMPOSSIBLE)
        return
    leave_after = (
        [None] if previous is None else _bounds(previous, previous.end, previous.official_end)
    )
    arrive_by = [b - rules.buffer for b in _bounds(target, target.start, target.official_start)]
    candidates: list[tuple[float, Journey, TripResult]] = []
    try:
        for after in leave_after:
            for before in arrive_by:
                if after is not None and utc(after) >= utc(before):
                    continue
                found = _trip(
                    routing,
                    origin,
                    destination,
                    depart_after=after,
                    arrive_before=before,
                    objective=Objective.LATEST_DEPARTURE,
                )
                if found.best is None:
                    continue
                journey = found.best
                late = max(0.0, _minutes(journey.arrival, official_deadline))
                early = 0.0
                if previous is not None:
                    early = max(
                        0.0, _minutes(previous.official_end or previous.end, journey.departure)
                    )
                cost = _minutes(official_deadline, journey.departure) + TOLERANCE_PER_MINUTE * (
                    late + early
                )
                candidates.append((cost, journey, found))
    except RoutingUnavailableError as exc:
        kept = rules.kept(origin, destination, official_start, True)
        if kept is not None and (not_before is None or utc(kept.departure) >= utc(not_before)):
            plan.issues.append(f"routing unavailable for {label}: previous kept ({exc})")
            block(
                kept.departure,
                _arrival_end(kept, official_start, rules.buffer),
                TravelStatus.STALE,
                kept,
            )
        else:
            plan.issues.append(f"routing unavailable for {label}: {exc}")
            block(gap_start, target.start, TravelStatus.UNKNOWN)
        return
    if not candidates:
        plan.issues.append(f"impossible connection {label}")
        block(gap_start, target.start, TravelStatus.IMPOSSIBLE)
        return
    # Cheapest first; on a tie the official bounds (tried first) win.
    _, journey, found = min(candidates, key=lambda c: c[0])
    block(
        journey.departure,
        _arrival_end(journey, official_start, rules.buffer),
        TravelStatus.ROUTED,
        journey,
        tuple(found.alternatives),
    )
    _journey_warnings(plan, journey, label)


def _arrival_end(journey: Journey, official_start: datetime, buffer: timedelta) -> datetime:
    """End of an outbound block: the official start, or later when tolerance is used (the
    user is there ``buffer`` after arriving)."""
    return max(official_start, journey.arrival + buffer, key=utc)


def _return_trip(
    routing: RoutingProvider,
    plan: TravelPlan,
    last: LocatedEvent,
    end_place: Place,
    return_by: datetime,
    rules: _Rules,
) -> None:
    """Way back after ``last``: leaving after its official end is preferred; leaving earlier
    within the tolerance costs ``TOLERANCE_PER_MINUTE`` per minute (see ``_outbound``)."""
    origin = last.place
    assert origin is not None  # noqa: S101 - unknown places are never routed
    official_end = last.official_end or last.end
    label = _label(origin, end_place, official_end)
    serves = (last.ref,) if last.ref else ()
    status = TravelStatus.IMPOSSIBLE
    journey: Journey | None = None
    alternatives: tuple[Journey, ...] = ()
    if utc(return_by) > utc(last.end):
        try:
            candidates: list[tuple[float, Journey, TripResult]] = []
            for after in _bounds(last, last.end, last.official_end):
                if utc(return_by) <= utc(after):
                    continue
                found = _trip(
                    routing,
                    origin,
                    end_place,
                    depart_after=after,
                    arrive_before=return_by,
                    objective=Objective.EARLIEST_ARRIVAL,
                )
                if found.best is not None:
                    early = max(0.0, _minutes(official_end, found.best.departure))
                    cost = _minutes(found.best.arrival, official_end) + TOLERANCE_PER_MINUTE * early
                    candidates.append((cost, found.best, found))
            if candidates:
                _, journey, found = min(candidates, key=lambda c: c[0])
                alternatives = tuple(found.alternatives)
        except RoutingUnavailableError as exc:
            kept = rules.kept(origin, end_place, official_end, False)
            if kept is not None and utc(kept.arrival) <= utc(return_by):
                plan.issues.append(f"routing unavailable for return {label}: previous kept")
                journey, status = kept, TravelStatus.STALE
            else:
                plan.issues.append(f"routing unavailable for return {label}: {exc}")
                status = TravelStatus.UNKNOWN
    if journey is not None:
        routed = TravelStatus.ROUTED if status is TravelStatus.IMPOSSIBLE else status
        plan.blocks.append(
            TravelBlock(
                min(official_end, journey.departure, key=utc),
                journey.arrival,
                origin,
                end_place,
                routed,
                journey,
                alternatives,
                returning=True,
                serves=serves,
                event_end=official_end,
            )
        )
        if routed is TravelStatus.ROUTED:
            _journey_warnings(plan, journey, label)
        return
    if status is TravelStatus.IMPOSSIBLE:
        plan.issues.append(f"no return journey found {label}")
    # Conservative block (no invented duration): bounded by the next known commitment and by
    # the longest acceptable journey.
    until = min(return_by, last.end + rules.max_journey, key=utc)
    if utc(until) > utc(last.end):
        plan.blocks.append(
            TravelBlock(
                last.end,
                until,
                origin,
                end_place,
                status,
                returning=True,
                serves=serves,
                event_end=official_end,
            )
        )


def plan_day_travel(
    events: Iterable[LocatedEvent],
    provider: RoutingProvider,
    *,
    buffer: timedelta = BUFFER,
    start_place: Place | None = None,
    end_place: Place | None = None,
    return_by: datetime | None = None,
    max_journey: timedelta = MAX_JOURNEY,
    previous: Previous | None = None,
) -> TravelPlan:
    """Journeys of ONE outing: from ``start_place`` to each located event in turn (directly,
    never via a residence) and back to ``end_place``, arriving before ``return_by`` (default:
    ``MAX_TRAVEL`` after the last event). ``start_place`` / ``end_place`` None: that leg is not
    planned (an unknown origin is reported by the caller)."""
    rules = _Rules(buffer, max_journey, previous)
    ordered = sorted((e for e in events if e.place is not None), key=lambda e: utc(e.start))
    plan = TravelPlan()
    if not ordered:
        return plan
    first = ordered[0]
    if start_place is not None and start_place != first.place:
        _outbound(provider, plan, start_place, first, None, rules)
    for before, current in itertools.pairwise(ordered):
        if before.place != current.place:
            assert before.place is not None  # noqa: S101 - filtered above
            _outbound(provider, plan, before.place, current, before, rules)
    last = max(ordered, key=lambda e: utc(e.end))
    if end_place is not None and end_place != last.place:
        _return_trip(provider, plan, last, end_place, return_by or last.end + MAX_TRAVEL, rules)
    return plan
