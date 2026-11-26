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


def friday_noon_alternation() -> Alternation:
    """'Friday 27/11 in the evening I go to B', then weekly: morning A/B, evening B/A."""
    return Alternation((OTHER, HOUSE), date(2026, 11, 27), time(12, 0), 7, TZ)


def local(day: int, hour: int, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=TZ)


def test_residences_switch_every_friday_at_noon() -> None:
    alt = friday_noon_alternation()
    assert alt.residence_at(local(27, 9)) == HOUSE  # Friday morning: previous residence
    assert alt.residence_at(local(27, 18)) == OTHER  # Friday evening: the declared one
    assert alt.residence_at(local(30, 8)) == OTHER
    assert alt.residence_at(local(4, 11, 12)) == OTHER
    assert alt.residence_at(local(4, 13, 12)) == HOUSE  # next Friday evening
    assert alt.residence_at(local(20, 10)) == OTHER  # before the statement: same rhythm back


def test_alternation_keeps_local_time_across_daylight_saving() -> None:
    alt = Alternation((OTHER, HOUSE), date(2026, 3, 20), time(12, 0), 7, TZ)  # DST on 29/03
    assert alt.switch_moment(1).astimezone(TZ).hour == 12
    assert alt.residence_at(datetime(2026, 3, 27, 11, 59, tzinfo=TZ)) == OTHER
    assert alt.residence_at(datetime(2026, 3, 27, 12, 0, tzinfo=TZ)) == HOUSE


def test_exceptions_win_without_shifting_the_recurrence() -> None:
    alt = friday_noon_alternation()
    stay = DeclaredStay(CAMPUS, local(1, 0, 12), local(2, 0, 12))  # one-off night elsewhere
    assert base_at(local(1, 20, 12), [stay], HOUSE, alt) == CAMPUS
    assert base_at(local(2, 20, 12), [stay], HOUSE, alt) == OTHER  # recurrence unchanged
    assert base_at(local(5, 20, 12), [stay], HOUSE, alt) == HOUSE
    assert base_at(local(5, 20, 12), [], HOUSE, None) == HOUSE  # no schedule: home


def test_alternation_needs_residences() -> None:
    with pytest.raises(ValueError, match="residences"):
        Alternation((), date(2026, 11, 27), time(12, 0), 7, TZ)


def test_temporary_access_applies_in_its_period_and_wins_over_the_default() -> None:
    station = Place("station", "Gare", Coordinates(50.25, 4.27))
    week = Access(HOUSE, station, timedelta(minutes=15), NINE, NINE + timedelta(days=7))
    inner = Recorder()
    adapter = AccessRouting(inner, [Access(HOUSE, STOP, timedelta(minutes=12)), week])
    adapter.plan(HOUSE.coordinates, CAMPUS.coordinates, NINE + timedelta(days=1), arrive_by=False)
    adapter.plan(HOUSE.coordinates, CAMPUS.coordinates, NINE + timedelta(days=8), arrive_by=False)
    assert inner.calls[0]["origin"] == station.coordinates  # inside the period
    assert inner.calls[1]["origin"] == STOP.coordinates  # after it: the default again
