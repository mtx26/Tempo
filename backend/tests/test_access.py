"""Transport access points, realistic journeys and the residence alternation (generic data)."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tempo.transport.access import ACCESS_MODE, Access, AccessRouting
from tempo.transport.location import Alternation, DeclaredStay, base_at
from tempo.transport.places import Coordinates, Place
from tempo.transport.routing import Journey, Leg

TZ = ZoneInfo("Europe/Brussels")
HOUSE = Place("house_a", "Maison A", Coordinates(50.20, 4.30))
STOP = Place("stop_a", "Arrêt A", Coordinates(50.24, 4.24))
OTHER = Place("house_b", "Maison B", Coordinates(50.37, 4.10))
CAMPUS = Place("campus", "Campus", Coordinates(50.46, 3.95))
NINE = datetime(2026, 12, 7, 8, 0, tzinfo=UTC)


def bus(start: datetime, minutes: int) -> Journey:
    end = start + timedelta(minutes=minutes)
    return Journey(
        (Leg("BUS", "x", "y", start, end, start, end, realtime=False),), start, end, 0, "t"
    )


class Recorder:
    """Inner provider: records queries, answers one bus of ``minutes`` around the query time."""

    def __init__(self, minutes: int = 60, *, motis: bool = False) -> None:
        self.minutes = minutes
        self.calls: list[dict[str, Any]] = []
        if motis:
            self.accepts_max_travel = True

    def plan(
        self, origin: Coordinates, destination: Coordinates, time: datetime, **kw: Any
    ) -> list[Journey]:
        self.calls.append({"origin": origin, "destination": destination, "time": time, **kw})
        if kw["arrive_by"]:
            return [bus(time - timedelta(minutes=self.minutes), self.minutes)]
        return [bus(time, self.minutes)]


def routing(inner: Recorder, **kw: Any) -> AccessRouting:
    return AccessRouting(inner, [Access(HOUSE, STOP, timedelta(minutes=12))], **kw)


def test_leaving_a_place_with_access_starts_from_the_access_point() -> None:
    inner = Recorder()
    (journey,) = routing(inner).plan(HOUSE.coordinates, CAMPUS.coordinates, NINE, arrive_by=False)
    assert inner.calls[0]["origin"] == STOP.coordinates
    assert inner.calls[0]["time"] == NINE + timedelta(minutes=12)  # transit after the access
    assert journey.legs[0].mode == ACCESS_MODE
    assert (journey.departure, journey.legs[0].end) == (NINE, NINE + timedelta(minutes=12))
    assert journey.duration == timedelta(minutes=72)
    assert not journey.legs[0].is_transit


def test_arriving_at_a_place_with_access_ends_after_the_access() -> None:
    inner = Recorder()
    (journey,) = routing(inner).plan(CAMPUS.coordinates, HOUSE.coordinates, NINE, arrive_by=True)
    assert inner.calls[0]["destination"] == STOP.coordinates
    assert inner.calls[0]["time"] == NINE - timedelta(minutes=12)
    assert journey.legs[-1].mode == ACCESS_MODE and journey.arrival == NINE


def test_gps_next_to_the_place_uses_its_access_but_not_further_away() -> None:
    inner = Recorder()
    near = Coordinates(50.2005, 4.3005)  # ~65 m from the house
    routing(inner).plan(near, CAMPUS.coordinates, NINE, arrive_by=False)
    routing(inner).plan(OTHER.coordinates, CAMPUS.coordinates, NINE, arrive_by=False)
    assert inner.calls[0]["origin"] == STOP.coordinates
    assert inner.calls[1]["origin"] == OTHER.coordinates


def test_places_without_access_are_routed_unchanged() -> None:
    inner = Recorder()
    (journey,) = routing(inner).plan(OTHER.coordinates, CAMPUS.coordinates, NINE, arrive_by=False)
    assert inner.calls[0]["time"] == NINE and len(journey.legs) == 1


def test_unrealistic_journeys_are_rejected_and_motis_limit_is_used() -> None:
    too_long = Recorder(minutes=14 * 60, motis=True)
    assert (
        routing(too_long, max_journey=timedelta(hours=3)).plan(
            OTHER.coordinates, CAMPUS.coordinates, NINE, arrive_by=False
        )
        == []
    )
    assert too_long.calls[0]["max_travel"] == timedelta(hours=3)  # MOTIS maxTravelTime
    from_house = Recorder(motis=True)
    routing(from_house, max_journey=timedelta(hours=3)).plan(
        HOUSE.coordinates, CAMPUS.coordinates, NINE, arrive_by=False
    )
    assert from_house.calls[0]["max_travel"] == timedelta(hours=3) - timedelta(minutes=12)
    plain = Recorder()  # a provider without the MOTIS option never receives it
    routing(plain).plan(OTHER.coordinates, CAMPUS.coordinates, NINE, arrive_by=False)
    assert "max_travel" not in plain.calls[0]


# --- residence alternation ------------------------------------------------------------------


def friday_alternation(**kw: object) -> Alternation:
    """'Friday 27/11 I go to B after class', then weekly."""
    return Alternation((OTHER, HOUSE), date(2026, 11, 27), 7, TZ, **kw)  # type: ignore[arg-type]


def local(day: int, hour: int, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=TZ)


def test_residence_changes_after_the_last_class_of_the_change_day() -> None:
    """C12: the change moment comes from the timetable (resolved by the caller)."""
    alt = friday_alternation(
        switches={date(2026, 11, 27): local(27, 16), date(2026, 12, 4): local(4, 12, 12)}
    )
    assert alt.residence_at(local(27, 9)) == HOUSE  # Friday before the last class
    assert alt.residence_at(local(27, 17)) == OTHER  # after it: the next residence
    assert alt.residence_at(local(30, 8)) == OTHER
    assert alt.residence_at(local(4, 11, 12)) == OTHER
    assert alt.residence_at(local(4, 13, 12)) == HOUSE
    assert alt.residence_at(local(19, 10)) == OTHER  # before the statement: same rhythm back


def test_change_day_without_known_moment_is_unknown_and_asked() -> None:
    """C12: no class and no stated time: never an arbitrary fixed time."""
    alt = friday_alternation()
    assert alt.residence_at(local(27, 9)) is None
    assert alt.residence_at(local(28, 9)) == OTHER and alt.residence_at(local(26, 9)) == HOUSE
    assert alt.unknown_days(local(26, 0), local(5, 0, 12)) == [
        date(2026, 11, 27),
        date(2026, 12, 4),
    ]
    single = Alternation((HOUSE,), date(2026, 11, 27), 7, TZ)
    assert (
        single.residence_at(local(27, 9)) == HOUSE
        and single.unknown_days(local(26, 0), local(5, 0, 12)) == []
    )


def test_a_stated_time_for_every_change_keeps_local_time_across_daylight_saving() -> None:
    alt = Alternation((OTHER, HOUSE), date(2026, 3, 20), 7, TZ, switch_at=time(18, 0))
    moment = alt.switch_moment(1)
    assert moment is not None and moment.astimezone(TZ).hour == 18
    assert alt.residence_at(datetime(2026, 3, 27, 17, 59, tzinfo=TZ)) == OTHER
    assert alt.residence_at(datetime(2026, 3, 27, 18, 0, tzinfo=TZ)) == HOUSE


def test_exceptions_win_without_shifting_the_recurrence() -> None:
    alt = friday_alternation(switch_at=time(18, 0))
    stay = DeclaredStay(CAMPUS, local(1, 0, 12), local(2, 0, 12))  # one-off night elsewhere
    assert base_at(local(1, 20, 12), [stay], alt) == CAMPUS
    assert base_at(local(2, 20, 12), [stay], alt) == OTHER  # recurrence unchanged
    assert base_at(local(5, 20, 12), [stay], alt) == HOUSE


def test_alternation_needs_residences() -> None:
    with pytest.raises(ValueError, match="residences"):
        Alternation((), date(2026, 11, 27), 7, TZ)


def test_temporary_access_applies_in_its_period_and_wins_over_the_default() -> None:
    station = Place("station", "Gare", Coordinates(50.25, 4.27))
    week = Access(HOUSE, station, timedelta(minutes=15), NINE, NINE + timedelta(days=7))
    inner = Recorder()
    adapter = AccessRouting(inner, [Access(HOUSE, STOP, timedelta(minutes=12)), week])
    adapter.plan(HOUSE.coordinates, CAMPUS.coordinates, NINE + timedelta(days=1), arrive_by=False)
    adapter.plan(HOUSE.coordinates, CAMPUS.coordinates, NINE + timedelta(days=8), arrive_by=False)
    assert inner.calls[0]["origin"] == station.coordinates  # inside the period
    assert inner.calls[1]["origin"] == STOP.coordinates  # after it: the default again
