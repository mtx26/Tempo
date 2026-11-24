"""The single itinerary service: bounds, context objective, criteria, paging, alternatives."""

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tempo.transport.itinerary import (
    MAX_PAGES,
    CachedRouting,
    Objective,
    TripQuery,
    find_trips,
)
from tempo.transport.places import Coordinates
from tempo.transport.routing import Journey, Leg, RoutingUnavailableError

TZ = ZoneInfo("Europe/Brussels")
A, B = Coordinates(50.85, 4.35), Coordinates(50.67, 4.61)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 12, 8, hour, minute, tzinfo=TZ)


def journey(start: datetime, minutes: int, transfers: int = 0, walk: int = 0) -> Journey:
    legs = []
    cursor = start
    if walk:
        legs.append(
            Leg(
                "WALK",
                "a",
                "s",
                cursor,
                cursor + timedelta(minutes=walk),
                cursor,
                cursor + timedelta(minutes=walk),
                realtime=False,
            )
        )
        cursor = legs[-1].end
    end = start + timedelta(minutes=minutes)
    legs.append(Leg("BUS", "s", "b", cursor, end, cursor, end, realtime=False))
    return Journey(tuple(legs), start, end, transfers, "fake")


class Timetable:
    """A departure every 15 min from 06:00, at most 3 per answer (like MOTIS). 09:45 is fast
    (20 min, 2 transfers, 15 min walk); others take 50 min direct."""

    def __init__(self, fail_after: int | None = None) -> None:
        self.calls: list[tuple[datetime, bool]] = []
        self.fail_after = fail_after
        self.slots = [
            journey(s, 20, 2, 15) if s == at(9, 45) else journey(s, 50)
            for s in (at(6) + timedelta(minutes=15 * i) for i in range(60))
        ]

    def plan(
        self, origin: Coordinates, destination: Coordinates, time: datetime, *, arrive_by: bool
    ) -> list[Journey]:
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RoutingUnavailableError("down")
        self.calls.append((time, arrive_by))
        if arrive_by:
            return [j for j in self.slots if j.arrival <= time][-3:]
        return [j for j in self.slots if j.departure >= time][:3]


def test_latest_departure_for_an_appointment_needs_one_request() -> None:
    provider = Timetable()
    result = find_trips(
        provider, TripQuery(A, B, arrive_before=at(10), objective=Objective.LATEST_DEPARTURE)
    )
    # 09:15 and 09:45 arrive at 10:05: the latest departure that keeps the deadline is 09:00.
    assert result.best is not None and result.best.departure == at(9)
    assert result.best.arrival <= at(10)
    assert len(provider.calls) == 1 and provider.calls[0][1] is True
    assert result.alternatives and all(j.arrival <= at(10) for j in result.alternatives)


def test_way_back_with_a_deadline_takes_the_earliest_arrival() -> None:
    """A return trip is not unbounded: it must arrive before the next commitment."""
    query = TripQuery(
        A, B, depart_after=at(8), arrive_before=at(9), objective=Objective.EARLIEST_ARRIVAL
    )
    result = find_trips(Timetable(), query)
    assert result.best is not None and result.best.departure == at(8)
    tight = TripQuery(
        A, B, depart_after=at(8), arrive_before=at(8, 30), objective=Objective.EARLIEST_ARRIVAL
    )
    assert find_trips(Timetable(), tight).best is None  # 50 min trips: impossible, not invented


def test_shortest_explores_the_whole_window_and_keeps_alternatives() -> None:
    provider = Timetable()
    result = find_trips(provider, TripQuery(A, B, depart_after=at(9), arrive_before=at(11)))
    assert result.best is not None and result.best.departure == at(9, 45)
    assert 1 < result.pages <= MAX_PAGES and len(result.alternatives) == 3


def test_criteria_exclude_and_are_reported() -> None:
    window = TripQuery(A, B, depart_after=at(9, 40), arrive_before=at(10, 10))
    assert find_trips(Timetable(), window).best is not None
    strict = find_trips(Timetable(), replace(window, max_transfers=0))
    assert strict.best is None and strict.excluded_by_criteria
    walk = find_trips(
        Timetable(), TripQuery(A, B, max_walk=timedelta(minutes=10), depart_after=at(9))
    )
    assert walk.best is not None and walk.best.transfers == 0


def test_single_bound_stays_within_its_span() -> None:
    result = find_trips(Timetable(), TripQuery(A, B, arrive_before=at(14)))
    assert result.best is not None and result.best.arrival >= at(12)
    wide = find_trips(Timetable(), TripQuery(A, B, arrive_before=at(14), span=timedelta(hours=6)))
    assert wide.best is not None and wide.best.departure == at(9, 45)


def test_outage_first_page_raises_later_page_is_incomplete() -> None:
    with pytest.raises(RoutingUnavailableError):
        find_trips(Timetable(fail_after=0), TripQuery(A, B, depart_after=at(9)))
    partial = find_trips(
        Timetable(fail_after=1), TripQuery(A, B, depart_after=at(9), arrive_before=at(12))
    )
    assert partial.incomplete and partial.best is not None


def test_queries_are_validated() -> None:
    with pytest.raises(ValueError, match="minimum departure"):
        TripQuery(A, B)
    with pytest.raises(ValueError, match="empty"):
        TripQuery(A, B, depart_after=at(10), arrive_before=at(9))


def test_cached_routing_calls_the_provider_once_per_request() -> None:
    provider = Timetable()
    cached = CachedRouting(provider)
    for _ in range(3):
        cached.plan(A, B, at(9), arrive_by=False)
    assert len(provider.calls) == 1
