"""Regression tests from the adversarial transport review."""

from datetime import UTC, datetime, timedelta

import pytest

from tempo.scheduler.model import Interval
from tempo.transport.location import (
    DeclaredStay,
    LocatedEvent,
    PhonePosition,
)
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
MUM = Place("mum", "Maman", Coordinates(50.4, 4.4))
FAR = Place("far", "Loin", Coordinates(49.6, 6.1))


def at(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 12, day, hour, minute, tzinfo=UTC)


def leg(start: datetime, end: datetime, mode: str = "BUS", realtime: bool = False) -> Leg:
    return Leg(mode, "a", "b", start, end, start, end, realtime=realtime)


class Scripted:
    """Provider returning a fixed list (or raising) regardless of the query."""

    def __init__(self, journeys: list[Journey] | None = None, fail: bool = False) -> None:
        self.journeys = journeys or []
        self.fail = fail

    def plan(
        self, origin: Coordinates, destination: Coordinates, time: datetime, *, arrive_by: bool
    ) -> list[Journey]:
        if self.fail:
            raise RoutingUnavailableError("down")
        return self.journeys


# 1. inconsistent journeys ----------------------------------------------------------------------


def test_journey_arriving_before_departure_is_rejected() -> None:
    with pytest.raises(ValueError):
        Journey((leg(at(9, 30), at(9, 40)),), at(9, 30), at(8), transfers=0, source="x")


def test_journey_with_overlapping_legs_is_rejected() -> None:
    legs = (leg(at(8), at(9)), leg(at(8, 30), at(9, 30)))
    with pytest.raises(ValueError):
        Journey(legs, at(8), at(9, 30), transfers=1, source="x")


def test_journey_legs_outside_its_bounds_are_rejected() -> None:
    with pytest.raises(ValueError):
        Journey((leg(at(7), at(9)),), at(8), at(9), transfers=0, source="x")


def test_journey_departing_after_event_start_is_never_routed() -> None:
    late = Journey((leg(at(9, 30), at(9, 50)),), at(9, 30), at(9, 50), 0, "x")
    events = [LocatedEvent(at(7), at(8), HOME), LocatedEvent(at(9), at(10), CAMPUS)]
    plan = plan_day_travel(events, Scripted([late]))
    (block,) = plan.blocks
    assert block.status is TravelStatus.IMPOSSIBLE
    assert [Interval(b.start, b.end) for b in plan.blocks] == [Interval(at(8), at(9))]


# 2. return trip failures block time ----------------------------------------------------------


def test_missing_return_journey_blocks_time_after_last_event() -> None:
    plan = plan_day_travel([LocatedEvent(at(19), at(22), CAMPUS)], Scripted([]), end_place=HOME)
    (block,) = plan.blocks
    assert block.status is TravelStatus.IMPOSSIBLE
    assert block.start == at(22)
    assert block.end > at(22)


def test_return_routing_outage_blocks_time_after_last_event() -> None:
    events = [LocatedEvent(at(19), at(22), CAMPUS)]
    plan = plan_day_travel(events, Scripted(fail=True), end_place=HOME)
    (block,) = plan.blocks
    assert block.status is TravelStatus.UNKNOWN
    assert block.start == at(22)


# 3. long trips beyond the old 3 h look-around ------------------------------------------------


def test_long_trip_far_after_window_still_shrinks_it() -> None:
    long_trip = Journey((leg(at(12), at(16, 50)),), at(12), at(16, 50), 0, "x")
    back = Journey((leg(at(18), at(22, 50)),), at(18), at(22, 50), 0, "x")
    result = travel_aware_study_windows(
        [StudyWindow(Interval(at(10), at(14)), HOME)],
        [LocatedEvent(at(17, 5), at(18), FAR)],
        Scripted([long_trip, back]),
    )
    assert [(w.start, w.end) for w in result.windows] == [(at(10), at(12))]


# 4/5/9. origin priorities --------------------------------------------------------------------


# 6. partial blocking instead of dropping a whole study window -------------------------------


class OutboundOnly:
    def __init__(self, journey: Journey) -> None:
        self.journey = journey

    def plan(
        self, origin: Coordinates, destination: Coordinates, time: datetime, *, arrive_by: bool
    ) -> list[Journey]:
        return [self.journey] if arrive_by else []


def test_missing_return_only_blocks_after_the_event() -> None:
    there = Journey((leg(at(18), at(18, 50)),), at(18), at(18, 50), 0, "x")
    result = travel_aware_study_windows(
        [StudyWindow(Interval(at(15), at(23)), HOME)],
        [LocatedEvent(at(19), at(23), CAMPUS)],
        OutboundOnly(there),
    )
    assert [(w.start, w.end) for w in result.windows] == [(at(15), at(18))]
    assert result.issues


# 7. first event without origin is reported ------------------------------------------------


def test_first_event_without_start_place_is_reported() -> None:
    plan = plan_day_travel([LocatedEvent(at(8), at(10), CAMPUS)], Scripted([]))
    assert any("origin" in issue for issue in plan.issues)


# 8. naive datetimes rejected ---------------------------------------------------------------


def test_naive_datetimes_are_rejected_in_transport_types() -> None:
    naive = datetime(2026, 12, 1, 8)
    with pytest.raises(ValueError):
        LocatedEvent(naive, naive + timedelta(hours=1), CAMPUS)
    with pytest.raises(ValueError):
        DeclaredStay(HOME, naive, naive + timedelta(hours=1))
    with pytest.raises(ValueError):
        PhonePosition(Coordinates(0, 0), 10.0, naive, "tracker")
