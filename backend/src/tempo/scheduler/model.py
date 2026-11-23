"""Input and output types of the deterministic study planner.

All datetimes are timezone-aware. Arithmetic is done in UTC: Python ignores UTC offsets when
subtracting or comparing two datetimes that share the same ``tzinfo`` object, which is wrong
across DST changes.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from tempo.domain.models import TargetKind


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if utc(self.end) <= utc(self.start):
            raise ValueError("interval end must be after start")

    def overlaps(self, other: "Interval") -> bool:
        return utc(self.start) < utc(other.end) and utc(other.start) < utc(self.end)

    def contains(self, other: "Interval") -> bool:
        return utc(self.start) <= utc(other.start) and utc(other.end) <= utc(self.end)


@dataclass(frozen=True, slots=True)
class StudyDemand:
    """Remaining study effort of one task, to be placed as movable blocks."""

    task_id: str
    subject: str
    remaining_minutes: int
    target: datetime
    """Instant by which the work should be done (end of the target day, local time)."""
    target_kind: TargetKind = TargetKind.SOFT

    def __post_init__(self) -> None:
        if self.remaining_minutes < 0:
            raise ValueError("remaining_minutes must not be negative")
        utc(self.target)


@dataclass(frozen=True, slots=True)
class PlannedBlock:
    task_id: str
    subject: str
    start: datetime
    end: datetime
    locked: bool = False
    """Locked blocks are never moved by the optimizer (changing them needs approval)."""

    def __post_init__(self) -> None:
        if utc(self.end) <= utc(self.start):
            raise ValueError("block end must be after start")

    @property
    def interval(self) -> Interval:
        return Interval(self.start, self.end)

    @property
    def duration(self) -> timedelta:
        return utc(self.end) - utc(self.start)


@dataclass(frozen=True, slots=True)
class Preferences:
    """Soft study preferences; never stronger than feasibility (docs/REQUIREMENTS.md)."""

    preferred_block_minutes: int = 90
    min_block_minutes: int = 30
    max_block_minutes: int = 90
    granularity_minutes: int = 15
    min_gap_same_subject_minutes: int = 30
    max_daily_study_minutes: int = 360
    max_blocks_per_subject_per_day: int = 2

    def __post_init__(self) -> None:
        g = self.granularity_minutes
        values = (self.preferred_block_minutes, self.min_block_minutes, self.max_block_minutes)
        if g <= 0 or any(v <= 0 or v % g for v in values):
            raise ValueError("block lengths must be positive multiples of the granularity")
        if not self.min_block_minutes <= self.preferred_block_minutes <= self.max_block_minutes:
            raise ValueError("expected min <= preferred <= max block length")
        if self.max_blocks_per_subject_per_day < 1 or self.max_daily_study_minutes < 0:
            raise ValueError("invalid daily limits")


@dataclass(frozen=True, slots=True)
class PlanningProblem:
    horizon: Interval
    timezone: ZoneInfo
    availability: list[Interval]
    """Windows in which study is allowed (configured in Home Assistant)."""
    busy: list[Interval]
    """Fixed commitments (iCal classes, races, travel...) that study must not overlap."""
    demands: list[StudyDemand]
    locked_blocks: list[PlannedBlock]
    preferences: Preferences = field(default_factory=Preferences)

    def __post_init__(self) -> None:
        ids = [d.task_id for d in self.demands]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate task_id in demands")


class PlanStatus(StrEnum):
    COMPLETE = "complete"
    """All requested effort is planned."""
    PARTIAL = "partial"
    """A feasible plan exists but some effort could not be placed (see unplanned_minutes)."""
    NO_SOLUTION = "no_solution"
    """The solver produced no plan (e.g. time limit); nothing new is proposed."""


@dataclass(frozen=True, slots=True)
class PlanResult:
    status: PlanStatus
    blocks: list[PlannedBlock]
    unplanned_minutes: dict[str, int] = field(default_factory=dict)
    late_minutes: dict[str, int] = field(default_factory=dict)
    """Effort minutes of soft-target tasks planned in blocks ending after the target."""
    hard_deadline_misses: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    """Source conflicts to surface for approval (e.g. a class moved onto a locked block)."""
    solver_status: str = ""
