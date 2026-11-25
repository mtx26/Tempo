"""Shared time primitives: one implementation, used by every package."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tempo import timeutil
from tempo.timeutil import Interval, end_of_day, local_midnight, utc
from tempo.transport import departure, itinerary, location, routing, travel

TZ = ZoneInfo("Europe/Brussels")


def test_naive_datetimes_are_refused_with_their_name() -> None:
    with pytest.raises(ValueError, match="arrival must be timezone-aware"):
        utc(datetime(2026, 12, 1, 8), "arrival")


def test_every_transport_module_uses_the_single_conversion() -> None:
    """Regression: travel.py used to read a naive datetime in the machine's timezone."""
    for module in (travel, itinerary, departure, location, routing):
        assert vars(module)["utc"] is timeutil.utc, module.__name__
    with pytest.raises(ValueError):
        vars(travel)["utc"](datetime(2026, 12, 1, 8))


def test_day_bounds_are_local_and_dst_safe() -> None:
    assert local_midnight(date(2026, 12, 7), TZ) == datetime(2026, 12, 7, tzinfo=TZ)
    assert end_of_day(date(2026, 12, 7), TZ) == datetime(2026, 12, 8, tzinfo=TZ)
    # 25 October 2026: clocks go back, the local day lasts 25 hours.
    day = date(2026, 10, 25)
    assert utc(end_of_day(day, TZ)) - utc(local_midnight(day, TZ)) == timedelta(hours=25)


def test_interval_compares_in_utc() -> None:
    first = Interval(datetime(2026, 12, 1, 8, tzinfo=TZ), datetime(2026, 12, 1, 9, tzinfo=TZ))
    second = Interval(datetime(2026, 12, 1, 8, 30, tzinfo=TZ), datetime(2026, 12, 1, 10, tzinfo=TZ))
    assert first.overlaps(second) and not first.contains(second)
    with pytest.raises(ValueError, match="end must be after start"):
        Interval(first.end, first.start)
