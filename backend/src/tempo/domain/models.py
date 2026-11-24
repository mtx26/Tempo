"""Core planning domain objects.

Rules encoded here (see ARCHITECTURE.md):
- University iCal events are FIXED source events.
- Personal study work is FLEXIBLE; its target date is SOFT unless explicitly HARD.
All datetimes must be timezone-aware to keep scheduling DST-safe.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum


class TargetKind(StrEnum):
    SOFT = "soft"
    """Complete before the date if possible; never overrides feasibility."""
    HARD = "hard"
    """Explicit deadline stated by the user; violations must be reported, not hidden."""


class EventStatus(StrEnum):
    CONFIRMED = "confirmed"
    TENTATIVE = "tentative"
    CANCELLED = "cancelled"


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class FixedEvent:
    """An immovable event imported from a source calendar (one concrete occurrence)."""

    uid: str
    start: datetime
    end: datetime
    summary: str
    location: str | None = None
    recurrence_id: str | None = None
    status: EventStatus = EventStatus.CONFIRMED

    def __post_init__(self) -> None:
        _require_aware("start", self.start)
        _require_aware("end", self.end)
        if self.end.astimezone(UTC) <= self.start.astimezone(UTC):
            raise ValueError("end must be after start")

    @property
    def duration(self) -> timedelta:
        # Same-tzinfo subtraction ignores UTC offsets (wrong across DST): compare in UTC.
        return self.end.astimezone(UTC) - self.start.astimezone(UTC)

    @property
    def key(self) -> tuple[str, str | None]:
        """Stable identity of an occurrence across syncs (UID + RECURRENCE-ID)."""
        return (self.uid, self.recurrence_id)


@dataclass(frozen=True, slots=True)
class StudyTask:
    """User-defined study workload, scheduled as movable study blocks."""

    subject: str
    effort_minutes: int
    target_date: date
    target_kind: TargetKind = TargetKind.SOFT
    completed_minutes: int = 0

    def __post_init__(self) -> None:
        if self.effort_minutes <= 0:
            raise ValueError("effort_minutes must be positive")
        if self.completed_minutes < 0:
            raise ValueError("completed_minutes must not be negative")

    @property
    def remaining_minutes(self) -> int:
        return max(0, self.effort_minutes - self.completed_minutes)
