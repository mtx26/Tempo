"""Input and output types of the single deterministic planner (study blocks and flexible
activities in ONE CP-SAT model: no category is solved before another).

All datetimes are timezone-aware; time primitives (``utc``, ``Interval``) live in
``tempo.timeutil``.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from tempo.const import (
    ACTIVITY_HOURS,
    BREAK_MINUTES,
    GRID_MINUTES,
    MIN_SESSION_MINUTES,
    SAME_SUBJECT_GAP_MINUTES,
    SESSION_MINUTES,
)
from tempo.domain.models import TargetKind
from tempo.timeutil import Interval, utc


@dataclass(frozen=True, slots=True)
class StudyDemand:
    """Remaining study effort of one task, to be placed as movable blocks."""

    task_id: str
    subject: str
    remaining_minutes: int
    target: datetime | None
    """Instant by which the work must be done (end of the target day, local time). No block
    is ever placed after it, soft or strict (moving a deadline needs the user's approval).
    None: no deadline, the work uses the remaining availability."""
    target_kind: TargetKind = TargetKind.SOFT
    priority: int = 2
    """1 low, 2 normal, 3 high: scales the cost of leaving this work unplanned (only matters
    when there is not enough time for everything)."""
    not_before: datetime | None = None
    """No block before this instant (the user declined a same-day rearrangement)."""
    completed_minutes: int | None = None

    def __post_init__(self) -> None:
        if self.remaining_minutes < 0:
            raise ValueError("remaining_minutes must not be negative")
        if not 1 <= self.priority <= 3:
            raise ValueError("priority must be 1, 2 or 3")
        if self.target is not None:
            utc(self.target)


@dataclass(frozen=True, slots=True)
class PlannedBlock:
    task_id: str
    subject: str
    start: datetime
    end: datetime
    locked: bool = False
    """Kept exactly as is by the planner: locked by the user, under way, or pinned (an
    existing block of the current day: moving it needs the user's approval)."""
    ref: str | None = None
    """``block:<id>`` of a stored locked block (conflict proposals); None otherwise."""

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
    """Soft study preferences; never stronger than feasibility. Block lengths are bounds."""

    preferred_block_minutes: int = SESSION_MINUTES
    min_block_minutes: int = MIN_SESSION_MINUTES
    max_block_minutes: int = SESSION_MINUTES
    granularity_minutes: int = GRID_MINUTES
    break_minutes: int = BREAK_MINUTES
    """Preferred break between two study sessions (consecutive sessions stay allowed)."""
    min_gap_same_subject_minutes: int = SAME_SUBJECT_GAP_MINUTES
    """Two sessions of the same subject (or a session right after a class of that subject)
    closer than this are penalised: subjects alternate when alternatives exist."""

    def __post_init__(self) -> None:
        g = self.granularity_minutes
        values = (self.preferred_block_minutes, self.min_block_minutes, self.max_block_minutes)
        if g <= 0 or any(v <= 0 or v % g for v in values):
            raise ValueError("block lengths must be positive multiples of the granularity")
        if not self.min_block_minutes <= self.preferred_block_minutes <= self.max_block_minutes:
            raise ValueError("expected min <= preferred <= max block length")
        # Upper bounds keep the CP-SAT model small (model building is not time-limited).
        if g not in (5, 10, 15, 30):
            raise ValueError("granularity must be 5, 10, 15 or 30 minutes")
        if self.min_block_minutes < 15 or self.max_block_minutes > 240:
            raise ValueError("blocks must last between 15 and 240 minutes")
        if not 0 <= self.min_gap_same_subject_minutes <= 240:
            raise ValueError("same-subject gap must be between 0 and 240 minutes")
        if not 0 <= self.break_minutes <= 60:
            raise ValueError("break must be between 0 and 60 minutes")


STUDY_PREFERENCE_KEYS = frozenset(Preferences.__dataclass_fields__)
"""Stored ``study_preferences`` keys still meaningful (removed keys are ignored on load)."""


@dataclass(frozen=True, slots=True)
class TrainingPreferences:
    """The user's soft preferences for trainings (setting ``training_preferences``). Defaults
    are the user's validated choices: days without classes first, then mornings, then
    afternoons, evenings as a fallback; avoid the dark for outdoor sessions."""

    free_days: bool = True
    time_of_day: str | None = "morning"
    """Preferred period (``const.TIMES_OF_DAY``); None: no preference."""
    daylight: bool = True
    rest_days: int = 0
    """Calendar days the user requires between two trainings (0 = no rule; never invented)."""


TRAINING_PREFERENCE_KEYS = frozenset(TrainingPreferences.__dataclass_fields__)


@dataclass(frozen=True, slots=True)
class TemporaryPreference:
    """A preference that holds only in ``[start, end)`` and then expires by itself, without
    touching the permanent one (same mechanism for every category)."""

    start: datetime
    end: datetime
    training: TrainingPreferences | None = None
    activity_hours: tuple[time, time] | None = None


@dataclass(frozen=True, slots=True)
class FlexibleActivity:
    """A flexible activity to place (training, personal outing, task session)."""

    activity_id: str
    title: str
    duration: timedelta
    earliest_start: datetime
    latest_end: datetime
    preferred_start: datetime | None = None
    previous_start: datetime | None = None
    """Start in the published plan (stability)."""
    travel_before: timedelta = timedelta(0)
    travel_after: timedelta = timedelta(0)
    preferred_day: date | None = None
    """Coach's suggested day: a soft preference, never a constraint."""
    sequence_group: str | None = None
    """Sessions of one coach week (or of one task) are placed in ``sequence_index`` order."""
    sequence_index: int = 0
    rest_days: int = 0
    """Explicit recovery: calendar days required after the previous training."""
    allowed: tuple[Interval, ...] | None = None
    """When set, the session must lie inside one of these intervals (category windows,
    unavailability, presence at the residence): a hard rule."""
    training: bool = False
    """A training session: the user's training preferences and recovery apply to it."""
    outdoor: bool = True
    """Trainings only: the dark is avoided for outdoor sessions."""
    priority: int = 2
    deadline: bool = True
    """False for a task session without deadline (lower cost when left unplaced)."""

    def __post_init__(self) -> None:
        if self.duration <= timedelta(0):
            raise ValueError("activity duration must be positive")
        if utc(self.latest_end) <= utc(self.earliest_start):
            raise ValueError("activity window must not be empty")
        if not 1 <= self.priority <= 3:
            raise ValueError("priority must be 1, 2 or 3")
        for value in (self.preferred_start, self.previous_start):
            if value is not None:
                utc(value)


@dataclass(frozen=True, slots=True)
class PlacedActivity:
    activity_id: str
    start: datetime
    end: datetime
    reserved: Interval
    """Session plus travel before/after: what nothing else may overlap."""


@dataclass(frozen=True, slots=True)
class ClassSlot:
    """A university class or exam (training preferences, subject alternation)."""

    start: datetime
    end: datetime
    title: str


@dataclass(frozen=True, slots=True)
class PlanningProblem:
    horizon: Interval
    timezone: ZoneInfo
    availability: list[Interval]
    """Study availability (time only: the user studies wherever they are)."""
    busy: list[Interval]
    """Everything fixed: commitments, journeys, uncertain periods, reserved proposals."""
    demands: list[StudyDemand] = field(default_factory=list)
    activities: list[FlexibleActivity] = field(default_factory=list)
    fixed_blocks: list[PlannedBlock] = field(default_factory=list)
    """Locked, under-way and pinned blocks: kept as they are."""
    preferences: Preferences = field(default_factory=Preferences)
    previous_blocks: list[PlannedBlock] = field(default_factory=list)
    """Movable blocks of the published plan: moving one costs a stability penalty that
    decreases with its distance in time."""
    training: TrainingPreferences = field(default_factory=TrainingPreferences)
    temporary: list[TemporaryPreference] = field(default_factory=list)
    activity_hours: tuple[time, time] = ACTIVITY_HOURS
    classes: list[ClassSlot] = field(default_factory=list)
    sport_days: list[date] = field(default_factory=list)
    """Days of fixed trainings and races (recovery of the flexible ones)."""
    sun: Callable[[date], tuple[datetime, datetime]] | None = None

    def __post_init__(self) -> None:
        ids = [d.task_id for d in self.demands]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate task_id in demands")

    def training_at(self, at: datetime) -> TrainingPreferences:
        for item in reversed(self.temporary):
            if item.training is not None and utc(item.start) <= utc(at) < utc(item.end):
                return item.training
        return self.training

    def hours_on(self, day: date) -> tuple[time, time]:
        noon = datetime.combine(day, time(12), tzinfo=self.timezone)
        for item in reversed(self.temporary):
            if item.activity_hours is not None and utc(item.start) <= utc(noon) < utc(item.end):
                return item.activity_hours
        return self.activity_hours


class PlanStatus(StrEnum):
    COMPLETE = "complete"
    """All requested effort and every activity are placed."""
    PARTIAL = "partial"
    """A feasible plan exists but something could not be placed (reported)."""
    NO_SOLUTION = "no_solution"
    """The solver produced no plan in its time (inconclusive); nothing new is proposed."""


@dataclass(frozen=True, slots=True)
class StudyEffort:
    reserved_minutes: int
    before_deadline_minutes: int | None
    after_deadline_minutes: int
    remaining_minutes: int
    unplanned_minutes: int
    completed_minutes: int | None


def study_effort(demands: list[StudyDemand], blocks: list[PlannedBlock]) -> dict[str, StudyEffort]:
    slots: dict[str, list[PlannedBlock]] = {}
    for block in blocks:
        slots.setdefault(block.task_id, []).append(block)
    result = {}
    for demand in demands:
        reserved = before = 0
        for block in slots.get(demand.task_id, []):
            reserved += int(block.duration.total_seconds() // 60)
            if demand.target is not None:
                end = min(utc(block.end), utc(demand.target))
                before += max(0, int((end - utc(block.start)).total_seconds() // 60))
        result[demand.task_id] = StudyEffort(
            reserved,
            before if demand.target is not None else None,
            reserved - before if demand.target is not None else 0,
            demand.remaining_minutes,
            max(0, demand.remaining_minutes - reserved),
            demand.completed_minutes,
        )
    return result


@dataclass(frozen=True, slots=True)
class PlanResult:
    status: PlanStatus
    blocks: list[PlannedBlock]
    activities: list[PlacedActivity] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)
    """Activity ids left unplaced (reported, never forced)."""
    outside_hours: list[str] = field(default_factory=list)
    unplanned_minutes: dict[str, int] = field(default_factory=dict)
    hard_deadline_misses: list[str] = field(default_factory=list)
    """Tasks with a STRICT deadline and work left unplanned."""
    deadline_misses: list[str] = field(default_factory=list)
    """Tasks with any deadline (soft or strict) and work left unplanned: the remaining work is
    never placed after the deadline without the user's approval."""
    solver_status: str = ""
    objective: float | None = None
    study_effort: dict[str, StudyEffort] = field(default_factory=dict)
