"""Core planning domain objects.

Rules encoded here (see ARCHITECTURE.md):
- University iCal events are FIXED source events.
- Personal study work is FLEXIBLE; its target date is SOFT unless explicitly HARD.
All datetimes must be timezone-aware to keep scheduling DST-safe.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from tempo.timeutil import utc


class TargetKind(StrEnum):
    SOFT = "soft"
    """Complete before the date if possible; never overrides feasibility."""
    HARD = "hard"
    """Explicit deadline stated by the user; violations must be reported, not hidden."""


class EventStatus(StrEnum):
    CONFIRMED = "confirmed"
    TENTATIVE = "tentative"
    CANCELLED = "cancelled"


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
        utc(self.start, "start")
        utc(self.end, "end")
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
