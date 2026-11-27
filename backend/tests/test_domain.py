from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from tempo.db.models import RecurrenceSeriesRow
from tempo.domain.models import FixedEvent
from tempo.domain.ops import DomainError, end_series_before, occurrences, validate_rrule


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


@pytest.mark.parametrize("rule", ["freq=hourly", "RrUlE:FREQ=Minutely", "FREQ=secondly"])
def test_rrule_frequency_is_checked_case_insensitively(rule: str) -> None:
    with pytest.raises(DomainError, match="trop fréquente"):
        validate_rrule(rule)


@pytest.mark.parametrize(
    ("rule", "days"),
    [
        ("FREQ=DAILY;COUNT=2", [26, 27]),
        ("FREQ=DAILY;UNTIL=20270327T090000", [26, 27]),
        ("FREQ=DAILY;UNTIL=20270328T070000Z", [26, 27, 28]),
        ("FREQ=WEEKLY;COUNT=2", [26, 2]),
    ],
)
def test_series_preserves_wall_time_and_bounds_across_dst(rule: str, days: list[int]) -> None:
    tz = ZoneInfo("Europe/Brussels")
    start = datetime(2027, 3, 26, 9, tzinfo=tz)
    series = RecurrenceSeriesRow(timezone=tz.key, dtstart=start, rrule=rule)
    result = occurrences(series, start, start + timedelta(days=10))
    assert [item.start.day for item in result] == days
    assert all(item.start.hour == 9 and item.key.endswith("T09:00") for item in result)


def test_cut_series_can_still_expand_before_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    tz = ZoneInfo("Europe/Brussels")
    start = datetime(2027, 3, 26, 9, tzinfo=tz)
    series = RecurrenceSeriesRow(timezone=tz.key, dtstart=start, rrule="FREQ=DAILY")
    with Session() as session:
        monkeypatch.setattr(session, "scalars", lambda _: [])
        end_series_before(session, series, "2027-03-28T09:00", start)
    result = occurrences(series, start, start + timedelta(days=7))
    assert [item.key for item in result] == ["2027-03-26T09:00", "2027-03-27T09:00"]
