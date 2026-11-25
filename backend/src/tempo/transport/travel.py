"""Mandatory travel: journeys between located commitments and travel-aware study windows.

Conventions (ARCHITECTURE.md: travel is mandatory, never invented). Journeys are chosen by
the single itinerary service (``itinerary.find_trips``):
- Outbound trips must arrive ``buffer`` before the event and leave after the previous
  commitment; objective: latest departure (least waiting). The travel block spans from the
  chosen departure up to the event start (the margin is part of the block).
- Return trips leave after the last event and must arrive before ``return_by`` (the next
  commitment, or a bounded evening); objective: earliest arrival.
- Alerts never exclude a journey (MOTIS attaches network-wide notices); cancelled trips are
  already removed by the provider. Alternatives are kept on each block.
- Live delays and relevant severe notices of chosen journeys are reported as ``warnings``.
- No feasible journey ⇒ IMPOSSIBLE (reported, gap blocked). Routing outage ⇒ UNKNOWN (reported,
  gap blocked). No journey is ever fabricated.
"""

import itertools
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from tempo.scheduler.model import Interval
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

DEFAULT_BUFFER = timedelta(minutes=10)
MAX_TRAVEL = timedelta(hours=6)
"""Events further than this from a study window are not routed for it."""


class TravelStatus(StrEnum):
    ROUTED = "routed"
    IMPOSSIBLE = "impossible"
    UNKNOWN = "unknown"


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


@dataclass(slots=True)
class TravelPlan:
    blocks: list[TravelBlock] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    """Live delays and disruption notices on routed journeys (informative, not blocking)."""


@dataclass(frozen=True, slots=True)
class StudyWindow:
    interval: Interval
    place: Place


@dataclass(slots=True)
class StudyWindows:
    windows: list[Interval] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _u(value: datetime) -> datetime:
    return value.astimezone(UTC)


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
    return f"{origin.name} -> {destination.name} ({_u(when).isoformat()})"


def _journey_warnings(plan: TravelPlan, journey: Journey, label: str) -> None:
    notes = journey_notes(journey)
    for message in [*(f"{note} ({label})" for note in notes.specific), *notes.general]:
        if message not in plan.warnings:
            plan.warnings.append(message)


def _outbound(
    routing: RoutingProvider,
    plan: TravelPlan,
    origin: Place,
    target: LocatedEvent,
    not_before: datetime | None,
    buffer: timedelta,
) -> None:
    deadline = target.start - buffer
    gap_start = not_before if not_before is not None else target.start - buffer
    if not_before is not None and not_before >= deadline:
        plan.issues.append(f"impossible connection {_label(origin, target.place, deadline)}")
        plan.blocks.append(
            TravelBlock(gap_start, target.start, origin, target.place, TravelStatus.IMPOSSIBLE)
        )
        return
    try:
        found = _trip(
            routing,
            origin,
            target.place,
            depart_after=not_before,
            arrive_before=deadline,
            objective=Objective.LATEST_DEPARTURE,
        )
    except RoutingUnavailableError as exc:
        plan.issues.append(
            f"routing unavailable for {_label(origin, target.place, deadline)}: {exc}"
        )
        plan.blocks.append(
            TravelBlock(gap_start, target.start, origin, target.place, TravelStatus.UNKNOWN)
        )
        return
    journey = found.best
    if journey is None:
        plan.issues.append(f"impossible connection {_label(origin, target.place, deadline)}")
        plan.blocks.append(
            TravelBlock(gap_start, target.start, origin, target.place, TravelStatus.IMPOSSIBLE)
        )
        return
    plan.blocks.append(
        TravelBlock(
            journey.departure,
            target.start,
            origin,
            target.place,
            TravelStatus.ROUTED,
            journey,
            tuple(found.alternatives),
        )
    )
    _journey_warnings(plan, journey, _label(origin, target.place, deadline))


def _return_trip(
    routing: RoutingProvider,
    plan: TravelPlan,
    last: LocatedEvent,
    end_place: Place,
    return_by: datetime,
) -> None:
    label = _label(last.place, end_place, last.end)
    status = TravelStatus.IMPOSSIBLE
    journey: Journey | None = None
    alternatives: tuple[Journey, ...] = ()
    if return_by > last.end:
        try:
            found = _trip(
                routing,
                last.place,
                end_place,
                depart_after=last.end,
                arrive_before=return_by,
                objective=Objective.EARLIEST_ARRIVAL,
            )
            journey, alternatives = found.best, tuple(found.alternatives)
        except RoutingUnavailableError as exc:
            plan.issues.append(f"routing unavailable for return {label}: {exc}")
            status = TravelStatus.UNKNOWN
    if journey is not None:
        plan.blocks.append(
            TravelBlock(
                last.end,
                journey.arrival,
                last.place,
                end_place,
                TravelStatus.ROUTED,
                journey,
                alternatives,
                returning=True,
            )
        )
        _journey_warnings(plan, journey, label)
        return
    if status is TravelStatus.IMPOSSIBLE:
        plan.issues.append(f"no return journey found {label}")
    # Conservative block (not a journey, no invented duration): the return time is unknown,
    # so the time up to the next known bound stays free of planned work.
    plan.blocks.append(
        TravelBlock(last.end, return_by, last.place, end_place, status, returning=True)
    )


def plan_day_travel(
    events: Iterable[LocatedEvent],
    provider: RoutingProvider,
    *,
    buffer: timedelta = DEFAULT_BUFFER,
    start_place: Place | None = None,
    end_place: Place | None = None,
    return_by: datetime | None = None,
) -> TravelPlan:
    """Travel blocks connecting a day's located commitments (and optional start/end places).

    ``start_place`` is the base at the outing start (``location.base_at``); when it is
    missing the outbound trip to the first event cannot be planned and an issue is reported.
    The way back to ``end_place`` must arrive before ``return_by`` (default: ``MAX_TRAVEL``
    after the last event).
    """
    routing = provider
    ordered = sorted(events, key=lambda e: _u(e.start))
    plan = TravelPlan()
    if not ordered:
        return plan
    if start_place is None:
        plan.issues.append(
            f"origin unknown before {ordered[0].place.name} ({ordered[0].start.isoformat()}): "
            "outbound travel not planned"
        )
    elif start_place != ordered[0].place:
        _outbound(routing, plan, start_place, ordered[0], None, buffer)
    for previous, current in itertools.pairwise(ordered):
        if previous.place != current.place:
            _outbound(routing, plan, previous.place, current, previous.end, buffer)
    last = ordered[-1]
    if end_place is not None and end_place != last.place:
        _return_trip(routing, plan, last, end_place, return_by or last.end + MAX_TRAVEL)
    return plan


def _subtract(window: Interval, busy: list[tuple[datetime, datetime]]) -> list[Interval]:
    pieces: list[Interval] = []
    cursor = _u(window.start)
    end = _u(window.end)
    for b_start, b_end in sorted(busy):
        if b_end <= cursor or b_start >= end:
            continue
        if b_start > cursor:
            pieces.append(Interval(cursor, b_start))
        cursor = max(cursor, b_end)
    if cursor < end:
        pieces.append(Interval(cursor, end))
    return pieces


def travel_aware_study_windows(
    windows: Iterable[StudyWindow],
    events: Iterable[LocatedEvent],
    provider: RoutingProvider,
    *,
    buffer: timedelta = DEFAULT_BUFFER,
    max_travel: timedelta = MAX_TRAVEL,
) -> StudyWindows:
    """Shrink study windows so that studying at the window's place leaves time to reach each
    away commitment (arriving ``buffer`` early) and to come back afterwards.

    Events within ``max_travel`` of a window are considered. When a journey cannot be found
    (or routing is down), the window is conservatively blocked from its start up to the event
    (outbound) or from the event to its end (return) and an issue is reported.
    """
    routing = provider
    all_events = sorted(events, key=lambda e: _u(e.start))
    result = StudyWindows()
    for window in windows:
        w_start, w_end = _u(window.interval.start), _u(window.interval.end)
        busy: list[tuple[datetime, datetime]] = []
        for event in all_events:
            e_start, e_end = _u(event.start), _u(event.end)
            if e_start >= w_end + max_travel or e_end <= w_start - max_travel:
                continue
            busy.append((e_start, e_end))
            if event.place == window.place:
                continue
            where = _label(window.place, event.place, event.start)
            try:
                there = _trip(
                    routing,
                    window.place,
                    event.place,
                    depart_after=None,
                    arrive_before=event.start - buffer,
                    objective=Objective.LATEST_DEPARTURE,
                ).best
            except RoutingUnavailableError as exc:
                result.issues.append(f"routing unavailable to {where}: {exc}")
                there = None
            try:
                back = _trip(
                    routing,
                    event.place,
                    window.place,
                    depart_after=event.end,
                    arrive_before=None,
                    objective=Objective.EARLIEST_ARRIVAL,
                ).best
            except RoutingUnavailableError as exc:
                result.issues.append(f"routing unavailable back from {where}: {exc}")
                back = None
            if there is None:
                result.issues.append(f"no outbound journey to {where}; time before it withheld")
                busy.append((min(w_start, e_start), e_start))
            else:
                busy.append((_u(there.departure), e_start))
            if back is None:
                result.issues.append(f"no return journey from {where}; time after it withheld")
                busy.append((e_end, max(w_end, e_end)))
            else:
                busy.append((e_end, _u(back.arrival)))
        result.windows.extend(_subtract(window.interval, busy))
    return result
