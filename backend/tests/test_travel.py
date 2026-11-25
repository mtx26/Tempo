"""Mandatory travel between located commitments and travel-aware study availability."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from tempo.timeutil import Interval
from tempo.transport.itinerary import CachedRouting
from tempo.transport.location import LocatedEvent
from tempo.transport.places import Coordinates, Place
from tempo.transport.routing import Journey, Leg, RoutingUnavailableError
from tempo.transport.travel import (
    StudyWindow,
    TravelStatus,
    plan_day_travel,
    travel_aware_study_windows,
)

HOME = Place("home", "Kot", Coordinates(50.668, 4.612))
CAMPUS = Place("campus", "Campus", Coordinates(50.669, 4.615))
POOL = Place("pool", "Piscine", Coordinates(50.85, 4.35))
BUFFER = timedelta(minutes=10)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 12, 1, hour, minute, tzinfo=UTC)


@dataclass
class FakeRouting:
    """Deterministic provider: door-to-door minutes per place pair, departures every 15 min."""

    minutes: dict[tuple[str, str], int]
    fail: bool = False
    calls: list[tuple[Coordinates, Coordinates, datetime, bool]] = field(default_factory=list)

    def _id(self, coords: Coordinates) -> str:
        return next(p.id for p in (HOME, CAMPUS, POOL) if p.coordinates == coords)

    def plan(
        self, origin: Coordinates, destination: Coordinates, time: datetime, *, arrive_by: bool
    ) -> list[Journey]:
        self.calls.append((origin, destination, time, arrive_by))
        if self.fail:
            raise RoutingUnavailableError("down")
        duration = timedelta(minutes=self.minutes[(self._id(origin), self._id(destination))])
        if arrive_by:
            arrival = time - timedelta(minutes=time.minute % 15)
            departure = arrival - duration
        else:
            departure = time + timedelta(minutes=-time.minute % 15)
            arrival = departure + duration
        leg = Leg("BUS", "a", "b", departure, arrival, departure, arrival, realtime=False)
        return [Journey((leg,), departure, arrival, transfers=0, source="fake")]


TIMES = {
    ("home", "campus"): 20,
    ("campus", "home"): 20,
    ("campus", "pool"): 50,
    ("pool", "campus"): 50,
    ("home", "pool"): 60,
    ("pool", "home"): 60,
}


def located(place: Place, start: datetime, end: datetime) -> LocatedEvent:
    return LocatedEvent(start=start, end=end, place=place)


# --- travel between commitments --------------------------------------------------------------


def test_travel_block_is_inserted_between_events_at_different_places() -> None:
    events = [located(CAMPUS, at(8), at(10)), located(POOL, at(12), at(13))]
    plan = plan_day_travel(events, FakeRouting(TIMES), buffer=BUFFER, start_place=CAMPUS)
    (block,) = plan.blocks
    assert block.status is TravelStatus.ROUTED
    assert block.origin == CAMPUS
    assert block.destination == POOL
    assert block.end == at(12)  # travel + safety margin up to the event start
    assert block.journey is not None
    assert block.journey.arrival <= at(11, 50)
    assert plan.issues == []


def test_no_travel_between_events_at_the_same_place() -> None:
    events = [located(CAMPUS, at(8), at(10)), located(CAMPUS, at(11), at(12))]
    plan = plan_day_travel(events, FakeRouting(TIMES), buffer=BUFFER)
    assert plan.blocks == []


def test_impossible_connection_is_reported_and_gap_blocked() -> None:
    events = [located(CAMPUS, at(8), at(10)), located(POOL, at(10, 30), at(12))]
    plan = plan_day_travel(events, FakeRouting(TIMES), buffer=BUFFER)
    (block,) = plan.blocks
    assert block.status is TravelStatus.IMPOSSIBLE
    assert (block.start, block.end) == (at(10), at(10, 30))
    assert block.journey is None
    assert any("impossible" in issue for issue in plan.issues)


def test_routing_outage_is_reported_never_invented() -> None:
    events = [located(CAMPUS, at(8), at(10)), located(POOL, at(12), at(13))]
    plan = plan_day_travel(events, FakeRouting(TIMES, fail=True), buffer=BUFFER)
    (block,) = plan.blocks
    assert block.status is TravelStatus.UNKNOWN
    assert block.journey is None
    assert (block.start, block.end) == (at(10), at(12))
    assert any("unavailable" in issue for issue in plan.issues)


def test_return_journey_to_end_place_is_planned() -> None:
    events = [located(POOL, at(18), at(19))]
    plan = plan_day_travel(events, FakeRouting(TIMES), buffer=BUFFER, end_place=HOME)
    (block,) = plan.blocks
    assert block.origin == POOL
    assert block.destination == HOME
    assert block.start == at(19)
    assert block.end == at(20)


def test_outbound_journey_from_start_place_is_planned() -> None:
    events = [located(CAMPUS, at(8), at(10))]
    plan = plan_day_travel(events, FakeRouting(TIMES), buffer=BUFFER, start_place=HOME)
    (block,) = plan.blocks
    assert block.origin == HOME
    assert block.end == at(8)
    assert block.start <= at(7, 30)


def test_travel_blocks_become_busy_intervals() -> None:
    events = [located(CAMPUS, at(8), at(10)), located(POOL, at(12), at(13))]
    plan = plan_day_travel(events, FakeRouting(TIMES), buffer=BUFFER)
    (busy,) = [Interval(b.start, b.end) for b in plan.blocks]
    assert isinstance(busy, Interval)
    assert busy.end == at(12)


def test_events_are_processed_in_chronological_order() -> None:
    events = [located(POOL, at(12), at(13)), located(CAMPUS, at(8), at(10))]
    plan = plan_day_travel(events, FakeRouting(TIMES), buffer=BUFFER)
    assert [b.destination for b in plan.blocks] == [POOL]


# --- travel-aware study availability ---------------------------------------------------------


def test_study_window_shrinks_by_travel_to_and_from_away_event() -> None:
    windows = [StudyWindow(Interval(at(8), at(18)), HOME)]
    events = [located(CAMPUS, at(10), at(12))]
    result = travel_aware_study_windows(windows, events, FakeRouting(TIMES), buffer=BUFFER)
    spans = [(w.start, w.end) for w in result.windows]
    # must arrive by 09:50 (10 min margin) -> fake arrival 09:45, departure 09:25;
    # return departs 12:00, home 12:20 (no margin needed after a return: no deadline)
    assert spans == [(at(8), at(9, 25)), (at(12, 20), at(18))]
    assert result.issues == []


def test_short_gap_between_away_events_is_not_studyable_at_home() -> None:
    windows = [StudyWindow(Interval(at(8), at(18)), HOME)]
    events = [located(CAMPUS, at(10), at(11)), located(CAMPUS, at(11, 45), at(13))]
    result = travel_aware_study_windows(windows, events, FakeRouting(TIMES), buffer=BUFFER)
    assert all(not (w.start < at(11, 45) and w.end > at(11)) for w in result.windows)


def test_study_at_event_place_needs_no_travel() -> None:
    windows = [StudyWindow(Interval(at(8), at(18)), CAMPUS)]
    events = [located(CAMPUS, at(10), at(12))]
    result = travel_aware_study_windows(windows, events, FakeRouting(TIMES), buffer=BUFFER)
    assert [(w.start, w.end) for w in result.windows] == [(at(8), at(10)), (at(12), at(18))]


def test_routing_outage_removes_affected_window_and_reports() -> None:
    windows = [StudyWindow(Interval(at(8), at(18)), HOME)]
    events = [located(CAMPUS, at(10), at(12))]
    result = travel_aware_study_windows(
        windows, events, FakeRouting(TIMES, fail=True), buffer=BUFFER
    )
    assert result.windows == []
    assert any("unavailable" in issue for issue in result.issues)


def test_identical_routing_queries_are_cached() -> None:
    windows = [
        StudyWindow(Interval(at(8), at(10)), HOME),
        StudyWindow(Interval(at(13), at(18)), HOME),
    ]
    events = [located(CAMPUS, at(10, 30), at(12))]
    routing = FakeRouting(TIMES)
    # One planning run shares one cache (the engine wraps its provider in CachedRouting).
    travel_aware_study_windows(windows, events, CachedRouting(routing), buffer=BUFFER)
    assert len(routing.calls) == len(set(routing.calls))


# --- end to end with the planner ---------------------------------------------------------------


def test_planner_never_places_study_on_travel_or_away_time() -> None:
    from zoneinfo import ZoneInfo

    from tempo.domain.models import TargetKind
    from tempo.scheduler.model import PlanningProblem, PlanStatus, StudyDemand
    from tempo.scheduler.solver import solve
    from tempo.scheduler.validate import validate_plan

    events = [located(CAMPUS, at(9), at(11)), located(POOL, at(15), at(16))]
    routing = FakeRouting(TIMES)
    travel = plan_day_travel(events, routing, buffer=BUFFER, start_place=HOME, end_place=HOME)
    study = travel_aware_study_windows(
        [StudyWindow(Interval(at(7), at(21)), HOME)], events, routing, buffer=BUFFER
    )
    busy = [Interval(e.start, e.end) for e in events] + [
        Interval(b.start, b.end) for b in travel.blocks
    ]
    problem = PlanningProblem(
        horizon=Interval(at(0), at(23, 59)),
        timezone=ZoneInfo("UTC"),
        availability=study.windows,
        busy=busy,
        demands=[StudyDemand("analyse", "analyse", 180, at(23), TargetKind.SOFT)],
        locked_blocks=[],
    )
    result = solve(problem, time_limit_seconds=10)
    assert validate_plan(problem, result) == []
    assert result.status is PlanStatus.COMPLETE
    for block in result.blocks:
        assert not any(b.overlaps(block.interval) for b in busy)
        assert any(w.contains(block.interval) for w in study.windows)


def test_way_back_has_a_deadline_and_is_reported_when_it_cannot_be_kept() -> None:
    """The return trip is not unbounded: it must reach the base before ``return_by``."""
    routing = FakeRouting(TIMES)
    events = [located(POOL, at(9), at(10))]
    ok = plan_day_travel(events, routing, start_place=CAMPUS, end_place=CAMPUS, return_by=at(11))
    back = ok.blocks[-1]
    assert back.status is TravelStatus.ROUTED and back.end <= at(11)
    late = plan_day_travel(
        events, routing, start_place=CAMPUS, end_place=CAMPUS, return_by=at(10, 30)
    )
    assert late.blocks[-1].status is TravelStatus.IMPOSSIBLE
    assert any("no return journey" in issue for issue in late.issues)
