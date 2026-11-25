"""Time primitives shared by every package (one implementation, ARCHITECTURE.md §3).

All datetimes are timezone-aware. Arithmetic and comparisons are done in UTC: Python ignores
UTC offsets when subtracting or comparing two datetimes that share the same ``tzinfo`` object,
which is wrong across DST changes. Day boundaries are LOCAL wall-clock midnights in the given
timezone (DST-safe: built with ``datetime.combine``, never midnight + offset).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def utc(value: datetime, name: str = "datetime") -> datetime:
    """``value`` in UTC; a naive datetime is refused (never read in the machine's timezone)."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    """Start of ``day`` in local time."""
    return datetime.combine(day, time(0), tzinfo=tz)


def end_of_day(day: date, tz: ZoneInfo) -> datetime:
    """End of ``day`` in local time (next local midnight): when work due that day is due."""
    return local_midnight(day + timedelta(days=1), tz)


@dataclass(frozen=True, slots=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if utc(self.end, "interval end") <= utc(self.start, "interval start"):
            raise ValueError("interval end must be after start")

    def overlaps(self, other: "Interval") -> bool:
        return utc(self.start) < utc(other.end) and utc(other.start) < utc(self.end)

    def contains(self, other: "Interval") -> bool:
        return utc(self.start) <= utc(other.start) and utc(other.end) <= utc(self.end)
