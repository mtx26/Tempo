from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tempo.domain.models import FixedEvent


def aware(hour: int) -> datetime:
    return datetime(2026, 12, 1, hour, tzinfo=UTC)


def test_fixed_event_requires_timezone_aware_datetimes() -> None:
    with pytest.raises(ValueError):
        FixedEvent(
            uid="a",
            start=datetime(2026, 12, 1, 8),
            end=datetime(2026, 12, 1, 10),
            summary="Cours",
        )


def test_fixed_event_requires_end_after_start() -> None:
    with pytest.raises(ValueError):
        FixedEvent(uid="a", start=aware(10), end=aware(10), summary="Cours")


def test_fixed_event_duration() -> None:
    event = FixedEvent(uid="a", start=aware(8), end=aware(10), summary="Cours")
    assert event.duration == timedelta(hours=2)


def test_fixed_event_duration_is_elapsed_time_across_dst_change() -> None:
    brussels = ZoneInfo("Europe/Brussels")
    # 2026-10-25: clocks go back at 03:00 CEST -> 02:00 CET, so 01:00-04:00 lasts 4 hours.
    event = FixedEvent(
        uid="night",
        start=datetime(2026, 10, 25, 1, tzinfo=brussels),
        end=datetime(2026, 10, 25, 4, tzinfo=brussels),
        summary="Nuit",
    )
    assert event.duration == timedelta(hours=4)
