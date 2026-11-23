"""Mandatory travel: journeys between located commitments and travel-aware study windows.

Conventions (docs/REQUIREMENTS.md: travel is mandatory, never invented):
- Outbound trips must arrive ``buffer`` before the event; the travel block spans from the
  chosen departure up to the event start (the margin is part of the block).
- Return trips have no deadline: the block spans from the event end to the arrival.
- Among feasible journeys the latest departure wins (least waiting); only between equal
  departures do fewer timed severe notices, then fewer transfers, decide. Alerts never exclude
  a journey (MOTIS attaches network-wide notices); cancelled trips are already removed by the
  provider.
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
from tempo.transport.location import LocatedEvent
from tempo.transport.places import Coordinates, Place
from tempo.transport.routing import Journey, RoutingProvider, RoutingUnavailableError

DEFAULT_BUFFER = timedelta(minutes=10)
MAX_TRAVEL = timedelta(hours=6)
"""Events further than this from a study window are not routed for it."""
UNRESOLVED_RETURN_BLOCK = timedelta(hours=2)
"""Conservative time kept free after the last event when no return journey is known."""


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


@dataclass(slots=True)
class TravelPlan:
    blocks: list[TravelBlock] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    """Live delays and disruption notices on routed journeys (informative, not blocking)."""

    def busy_intervals(self) -> list[Interval]:
        return [Interval(b.start, b.end) for b in self.blocks if b.end > b.start]


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


class _CachedRouting:
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


def _arrive_by(
    routing: _CachedRouting,
    origin: Place,
    destination: Place,
    deadline: datetime,
    not_before: datetime | None,
) -> Journey | None:
    journeys = routing.plan(origin.coordinates, destination.coordinates, deadline, arrive_by=True)
    feasible = [
        j
        for j in journeys
        if _u(j.arrival) <= _u(deadline)
        and (not_before is None or _u(j.departure) >= _u(not_before))
    ]
    if not feasible:
        return None
    return max(feasible, key=lambda j: (_u(j.departure), -j.severe_disruptions, -j.transfers))


def _depart_after(
    routing: _CachedRouting, origin: Place, destination: Place, earliest: datetime
) -> Journey | None:
    journeys = routing.plan(origin.coordinates, destination.coordinates, earliest, arrive_by=False)
    feasible = [j for j in journeys if _u(j.departure) >= _u(earliest)]
    if not feasible:
        return None
    return min(feasible, key=lambda j: (_u(j.arrival), j.severe_disruptions, j.transfers))


def _label(origin: Place, destination: Place, when: datetime) -> str:
    return f"{origin.name} -> {destination.name} ({when.isoformat()})"


REPORTED_DELAY = timedelta(minutes=2)


def _warn(plan: TravelPlan, message: str) -> None:
    if message not in plan.warnings:
        plan.warnings.append(message)


def _journey_warnings(plan: TravelPlan, journey: Journey, label: str) -> None:
    for leg in journey.legs:
        late = max(leg.delay, leg.arrival_delay)
        if leg.is_transit and leg.realtime and late >= REPORTED_DELAY:
            minutes = int(late.total_seconds() // 60)
            _warn(plan, f"live delay +{minutes} min on {leg.line or leg.mode} ({label})")
    for alert in journey.timed_disruptions:
        if alert.severe:
            _warn(plan, f"disruption notice {alert.effect}: {alert.header} ({label})")
    agencies = sorted(
        {leg.agency or leg.mode for leg in journey.legs for a in leg.alerts if not a.impact_periods}
    )
    if any(alert.severe for alert in journey.untimed_disruptions):
        _warn(
            plan,
            f"operator notices without impact period ({', '.join(agencies)}) were not checked "
            "against the planned journeys",
        )


def _outbound(
    routing: _CachedRouting,
    plan: TravelPlan,
    origin: Place,
    target: LocatedEvent,
    not_before: datetime | None,
    buffer: timedelta,
) -> None:
    deadline = target.start - buffer
    gap_start = not_before if not_before is not None else target.start - buffer
    try:
        journey = _arrive_by(routing, origin, target.place, deadline, not_before)
    except RoutingUnavailableError as exc:
        plan.issues.append(
            f"routing unavailable for {_label(origin, target.place, deadline)}: {exc}"
        )
        plan.blocks.append(
            TravelBlock(gap_start, target.start, origin, target.place, TravelStatus.UNKNOWN)
        )
        return
    if journey is None:
        plan.issues.append(f"impossible connection {_label(origin, target.place, deadline)}")
        plan.blocks.append(
            TravelBlock(gap_start, target.start, origin, target.place, TravelStatus.IMPOSSIBLE)
        )
        return
    plan.blocks.append(
        TravelBlock(
            journey.departure, target.start, origin, target.place, TravelStatus.ROUTED, journey
        )
    )
    _journey_warnings(plan, journey, _label(origin, target.place, deadline))


def _return_trip(
    routing: _CachedRouting, plan: TravelPlan, last: LocatedEvent, end_place: Place
) -> None:
    label = _label(last.place, end_place, last.end)
    status = TravelStatus.IMPOSSIBLE
    try:
        journey = _depart_after(routing, last.place, end_place, last.end)
    except RoutingUnavailableError as exc:
        plan.issues.append(f"routing unavailable for return {label}: {exc}")
        journey, status = None, TravelStatus.UNKNOWN
    if journey is not None:
        plan.blocks.append(
            TravelBlock(
                last.end, journey.arrival, last.place, end_place, TravelStatus.ROUTED, journey
            )
        )
        _journey_warnings(plan, journey, label)
        return
    if status is TravelStatus.IMPOSSIBLE:
        plan.issues.append(f"no return journey found {label}")
    # Conservative block (not a journey): the return time is unknown, keep the evening free.
    plan.blocks.append(
        TravelBlock(last.end, last.end + UNRESOLVED_RETURN_BLOCK, last.place, end_place, status)
    )


def plan_day_travel(
    events: Iterable[LocatedEvent],
    provider: RoutingProvider,
    *,
    buffer: timedelta = DEFAULT_BUFFER,
    start_place: Place | None = None,
    end_place: Place | None = None,
) -> TravelPlan:
    """Travel blocks connecting a day's located commitments (and optional start/end places).

    ``start_place`` should come from ``location.infer_origin``; when it is missing the outbound
    trip to the first event cannot be planned and an issue is reported.
    """
    routing = _CachedRouting(provider)
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
        _return_trip(routing, plan, last, end_place)
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
    routing = _CachedRouting(provider)
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
                there = _arrive_by(routing, window.place, event.place, event.start - buffer, None)
            except RoutingUnavailableError as exc:
                result.issues.append(f"routing unavailable to {where}: {exc}")
                there = None
            try:
                back = _depart_after(routing, event.place, window.place, event.end)
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
