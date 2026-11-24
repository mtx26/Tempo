"""Departure re-check from the current phone position."""

from datetime import UTC, datetime, timedelta

import pytest

from tempo.transport.departure import DepartureStatus, check_departure
from tempo.transport.location import LocatedEvent, PhonePosition
from tempo.transport.places import Coordinates, Place
from tempo.transport.routing import Journey, Leg, RoutingUnavailableError, ServiceAlert

NOW = datetime(2026, 12, 1, 7, 30, tzinfo=UTC)
HERE = Coordinates(50.668, 4.612)
CAMPUS = Place("campus", "Campus", Coordinates(50.85, 4.35))
LECTURE = LocatedEvent(NOW.replace(hour=9, minute=0), NOW.replace(hour=11, minute=0), CAMPUS)


def fresh(coords: Coordinates = HERE, age: int = 2, accuracy: float = 20.0) -> PhonePosition:
    return PhonePosition(coords, accuracy, NOW - timedelta(minutes=age), "device_tracker.phone")


def trip(dep: datetime, minutes: int = 40, *, delay: int = 0, alerts=()) -> Journey:  # type: ignore[no-untyped-def]
    shift = timedelta(minutes=delay)
    end = dep + timedelta(minutes=minutes)
    leg = Leg(
        "REGIONAL_RAIL",
        "a",
        "b",
        dep + shift,
        end + shift,
        dep,
        end,
        realtime=True,
        line="IC",
        alerts=tuple(alerts),
    )
    return Journey((leg,), dep + shift, end + shift, 0, "test")


class Provider:
    def __init__(
        self, arrive_by: list[Journey], depart_at: list[Journey] | None = None, fail: bool = False
    ) -> None:
        self.arrive_by, self.depart_at, self.fail = arrive_by, depart_at or [], fail
        self.calls: list[tuple[datetime, bool]] = []

    def plan(
        self, origin: Coordinates, destination: Coordinates, time: datetime, *, arrive_by: bool
    ) -> list[Journey]:
        self.calls.append((time, arrive_by))
        if self.fail:
            raise RoutingUnavailableError("MOTIS down")
        return self.arrive_by if arrive_by else self.depart_at


def test_stale_inaccurate_or_missing_position_is_never_used() -> None:
    provider = Provider([trip(NOW.replace(hour=8))])
    for position in (None, fresh(age=30), fresh(accuracy=900.0)):
        advice = check_departure(LECTURE, position, provider, now=NOW)
        assert advice.status is DepartureStatus.UNKNOWN_POSITION
    assert not provider.calls


def test_already_at_the_event_place() -> None:
    near = Coordinates(50.8505, 4.3505)
    advice = check_departure(LECTURE, fresh(near), Provider([]), now=NOW)
    assert advice.status is DepartureStatus.ALREADY_THERE


def test_on_time_picks_the_latest_departure_keeping_the_buffer() -> None:
    early, late = trip(NOW.replace(hour=7, minute=50)), trip(NOW.replace(hour=8, minute=5))
    too_late = trip(NOW.replace(hour=8, minute=15))  # arrives 08:55 > 08:50
    advice = check_departure(LECTURE, fresh(), Provider([early, late, too_late]), now=NOW)
    assert advice.status is DepartureStatus.ON_TIME
    assert advice.journey is late
    assert advice.leave_at == NOW.replace(hour=8, minute=5)


def test_journeys_leaving_in_the_past_are_ignored() -> None:
    gone = trip(NOW - timedelta(minutes=5))
    soon = trip(NOW + timedelta(minutes=3))
    advice = check_departure(LECTURE, fresh(), Provider([gone, soon]), now=NOW)
    assert advice.status is DepartureStatus.LEAVE_NOW
    assert advice.journey is soon


def test_leave_earlier_than_planned_because_of_a_live_delay() -> None:
    delayed = trip(NOW.replace(hour=8, minute=5), delay=10)  # now arrives 08:55
    earlier = trip(NOW.replace(hour=7, minute=45))
    advice = check_departure(
        LECTURE,
        fresh(),
        Provider([delayed, earlier]),
        now=NOW,
        planned_departure=NOW.replace(hour=8, minute=5),
    )
    assert advice.status is DepartureStatus.LEAVE_EARLIER
    assert advice.leave_at == NOW.replace(hour=7, minute=45)


def test_at_risk_when_only_the_margin_is_lost() -> None:
    late_arrival = trip(NOW.replace(hour=8, minute=15))  # arrives 08:55, start 09:00
    advice = check_departure(LECTURE, fresh(), Provider([], [late_arrival]), now=NOW)
    assert advice.status is DepartureStatus.AT_RISK
    assert "5 min before the start" in advice.reason


def test_late_with_minutes() -> None:
    advice = check_departure(
        LECTURE, fresh(), Provider([], [trip(NOW.replace(hour=8, minute=30))]), now=NOW
    )
    assert advice.status is DepartureStatus.LATE
    assert advice.minutes_late == 10


def test_impossible_when_no_journey_at_all() -> None:
    advice = check_departure(LECTURE, fresh(), Provider([], []), now=NOW)
    assert advice.status is DepartureStatus.IMPOSSIBLE


def test_routing_outage_is_unknown_not_impossible() -> None:
    advice = check_departure(LECTURE, fresh(), Provider([], fail=True), now=NOW)
    assert advice.status is DepartureStatus.UNKNOWN_ROUTING
    assert "MOTIS down" in advice.reason


def test_event_already_started() -> None:
    later = NOW.replace(hour=9, minute=20)
    position = PhonePosition(HERE, 20.0, later, "device_tracker.phone")
    advice = check_departure(LECTURE, position, Provider([]), now=later)
    assert advice.status is DepartureStatus.LATE
    assert advice.minutes_late == 20


def test_inside_the_buffer_skips_arrive_by_and_checks_asap() -> None:
    now = NOW.replace(hour=8, minute=55)
    position = PhonePosition(HERE, 20.0, now, "device_tracker.phone")
    provider = Provider([], [])
    check_departure(LECTURE, position, provider, now=now)
    assert provider.calls == [(now, False)]


def test_warnings_report_delay_and_timed_disruption() -> None:
    alert = ServiceAlert(
        "Travaux",
        effect="DETOUR",
        impact_periods=((NOW, NOW + timedelta(hours=4)),),
    )
    journey = trip(NOW.replace(hour=7, minute=50), delay=4, alerts=[alert])
    advice = check_departure(LECTURE, fresh(), Provider([journey]), now=NOW)
    assert advice.warnings == [
        "retard en temps réel +4 min sur IC",
        "perturbation DETOUR : Travaux",
    ]


def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="aware"):
        check_departure(LECTURE, fresh(), Provider([]), now=NOW.replace(tzinfo=None))


def test_distance() -> None:
    brussels, liege = Coordinates(50.8455, 4.3570), Coordinates(50.6245, 5.5668)
    assert 88_000 < brussels.distance_m(liege) < 89_000
    assert brussels.distance_m(brussels) == 0
