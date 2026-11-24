"""PostgreSQL schema (SQLAlchemy 2 ORM). All instants are ``timestamptz``.

Migrations live in ``tempo/db/alembic``; ``tests/integration`` asserts they match this metadata.
"""

import uuid
from datetime import date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from tempo.domain.models import EventStatus, TargetKind

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Timestamp = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: Timestamp, dict[str, Any]: JSONB}


def _enum(enum_cls: type[EventStatus] | type[TargetKind], name: str) -> Enum:
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class CalendarSource(Base):
    """A configured iCal feed. The feed URL is configuration/secret, never stored here."""

    __tablename__ = "calendar_source"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FixedEventRow(Base):
    __tablename__ = "fixed_event"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "uid",
            "recurrence_id",
            name="occurrence",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("end_at > start_at", name="positive_duration"),
        Index("ix_fixed_event_active_start", "start_at", postgresql_where="deleted_at IS NULL"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calendar_source.id"))
    uid: Mapped[str] = mapped_column(String(512))
    recurrence_id: Mapped[str | None] = mapped_column(String(64))
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    summary: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    status: Mapped[EventStatus] = mapped_column(_enum(EventStatus, "event_status"))
    first_seen_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]
    """Set when the occurrence disappears from the source; rows are kept for history."""

    source: Mapped[CalendarSource] = relationship()


class SyncRun(Base):
    __tablename__ = "sync_run"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calendar_source.id"))
    started_at: Mapped[datetime]
    window_start: Mapped[datetime]
    window_end: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(20))
    added: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    changed: Mapped[int] = mapped_column(Integer, default=0)
    restored: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[dict[str, Any]] = mapped_column(default=dict)


class StudyTaskRow(Base):
    """A task: study work for a subject (``kind = 'study'``) or a personal task.

    ``effort_minutes`` NULL = duration unknown: the task is kept but never planned with a
    guessed length. ``target_date`` NULL = no deadline. ``completed_minutes`` is a cache of the
    sum of ``work_log`` rows, recomputed only by ``tempo.domain.ops.record_work``."""

    __tablename__ = "study_task"
    __table_args__ = (
        CheckConstraint("effort_minutes IS NULL OR effort_minutes > 0", name="positive_effort"),
        CheckConstraint("completed_minutes >= 0", name="non_negative_completed"),
        CheckConstraint("kind IN ('study', 'personal')", name="kind"),
        CheckConstraint("priority BETWEEN 1 AND 3", name="priority_range"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subject: Mapped[str] = mapped_column(String(200))
    """Study subject, or the title of a personal task."""
    effort_minutes: Mapped[int | None]
    completed_minutes: Mapped[int] = mapped_column(default=0, server_default="0")
    target_date: Mapped[date | None] = mapped_column(Date)
    target_kind: Mapped[TargetKind] = mapped_column(
        _enum(TargetKind, "target_kind"), default=TargetKind.SOFT, server_default="soft"
    )
    archived: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    kind: Mapped[str] = mapped_column(String(10), default="study", server_default="study")
    priority: Mapped[int] = mapped_column(default=2, server_default="2")
    """1 = low, 2 = normal, 3 = high."""
    splittable: Mapped[bool] = mapped_column(default=True, server_default="true")
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    place_id: Mapped[str | None] = mapped_column(ForeignKey("place.id", ondelete="SET NULL"))
    """A located task is planned as one flexible session (an ``activity`` of kind 'task')."""


class WorkLogRow(Base):
    """Work actually done on a task (a correction is a negative entry). Idempotent by key."""

    __tablename__ = "work_log"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("study_task.id", ondelete="CASCADE"))
    block_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("study_block.id", ondelete="SET NULL")
    )
    minutes: Mapped[int]
    recorded_at: Mapped[datetime]
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)


class StudyBlockRow(Base):
    __tablename__ = "study_block"
    __table_args__ = (CheckConstraint("end_at > start_at", name="positive_duration"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("study_task.id", ondelete="CASCADE"))
    schedule_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("schedule_version.id"))
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    locked: Mapped[bool] = mapped_column(default=False, server_default="false")
    state: Mapped[str] = mapped_column(String(20), default="planned", server_default="planned")


class ScheduleVersion(Base):
    __tablename__ = "schedule_version"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    state: Mapped[str] = mapped_column(String(20), default="proposed")
    solver_status: Mapped[str] = mapped_column(String(20))
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)


class AuditEntry(Base):
    __tablename__ = "audit_entry"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    at: Mapped[datetime]
    actor: Mapped[str] = mapped_column(String(50))
    action: Mapped[str] = mapped_column(String(50))
    entity: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class PhonePositionRow(Base):
    """Latest position of a configured HA phone tracker; only the latest fix is kept."""

    __tablename__ = "phone_position"
    __table_args__ = (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="latitude_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="longitude_range"),
        CheckConstraint("accuracy_m >= 0", name="accuracy_non_negative"),
    )
    entity_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    latitude: Mapped[float]
    longitude: Mapped[float]
    accuracy_m: Mapped[float]
    observed_at: Mapped[datetime]
    received_at: Mapped[datetime]


class PlaceRow(Base):
    """Named place with coordinates (home, campus, club...). Configured from Home Assistant."""

    __tablename__ = "place"
    __table_args__ = (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="latitude_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="longitude_range"),
    )
    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    latitude: Mapped[float]
    longitude: Mapped[float]


class PlaceAliasRow(Base):
    """Exact location text (e.g. iCal LOCATION) mapped to a place; normalised (casefold)."""

    __tablename__ = "place_alias"
    alias: Mapped[str] = mapped_column(String(500), primary_key=True)
    place_id: Mapped[str] = mapped_column(ForeignKey("place.id", ondelete="CASCADE"))


class AvailabilityRow(Base):
    """Weekly window in which study is allowed, at a place (local wall-clock time)."""

    __tablename__ = "availability_window"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        CheckConstraint("end_minute > start_minute", name="positive_duration"),
        CheckConstraint("start_minute >= 0 AND end_minute <= 1440", name="minute_range"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    weekday: Mapped[int]
    """0 = Monday."""
    start_minute: Mapped[int]
    end_minute: Mapped[int]
    place_id: Mapped[str] = mapped_column(ForeignKey("place.id", ondelete="CASCADE"))


class CategoryWindowRow(Base):
    """Weekly window in which flexible activities of one category may be placed (B07).

    A category with at least one window is restricted to its windows (hard rule); a category
    without windows is only bounded by the daily activity hours."""

    __tablename__ = "category_window"
    __table_args__ = (
        CheckConstraint("kind IN ('training', 'personal', 'task')", name="kind"),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        CheckConstraint("end_minute > start_minute", name="positive_duration"),
        CheckConstraint("start_minute >= 0 AND end_minute <= 1440", name="minute_range"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(10))
    weekday: Mapped[int]
    """0 = Monday."""
    start_minute: Mapped[int]
    end_minute: Mapped[int]


def _default_scheduling(context: Any) -> str:
    return "fixed" if context.get_current_parameters().get("kind") == "race" else "flexible"


class ActivityRow(Base):
    """A planned activity: training (flexible, prescribed content kept), race (fixed), personal
    event (fixed appointment or flexible outing) or a located task session.

    Category (``kind``), scheduling mode and lock are independent. Fixed activities occupy
    ``earliest_start_at`` .. ``latest_end_at``; flexible ones move inside that window and their
    chosen start, ``planned_start_at``, belongs to the plan."""

    __tablename__ = "activity"
    __table_args__ = (
        CheckConstraint("kind IN ('training', 'race', 'personal', 'task')", name="kind"),
        CheckConstraint("scheduling IN ('fixed', 'flexible')", name="scheduling"),
        CheckConstraint("kind <> 'race' OR scheduling = 'fixed'", name="race_fixed"),
        CheckConstraint("priority BETWEEN 1 AND 3", name="priority_range"),
        UniqueConstraint("series_id", "occurrence_key", name="series_occurrence"),
        CheckConstraint(
            "duration_minutes IS NULL OR duration_minutes > 0", name="positive_duration"
        ),
        CheckConstraint(
            "duration_minutes IS NOT NULL OR status = 'to_confirm'", name="duration_known"
        ),
        CheckConstraint("latest_end_at > earliest_start_at", name="window"),
        UniqueConstraint("source", "source_ref", name="source_ref"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(300))
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    """Prescribed content, preserved verbatim (e.g. from the coach's e-mail)."""
    duration_minutes: Mapped[int | None]
    """None only while ``status = 'to_confirm'`` (coach plan without explicit total duration)."""
    earliest_start_at: Mapped[datetime]
    latest_end_at: Mapped[datetime]
    """Races: exactly start + duration. Trainings: the window they may move in."""
    preferred_start_at: Mapped[datetime | None]
    place_id: Mapped[str | None] = mapped_column(ForeignKey("place.id", ondelete="SET NULL"))
    planned_start_at: Mapped[datetime | None]
    locked: Mapped[bool] = mapped_column(default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    source: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    user_modified: Mapped[bool] = mapped_column(default=False, server_default="false")
    """Edited by the user: a re-sent coach plan never overwrites it silently."""
    sequence_group: Mapped[str | None] = mapped_column(String(100))
    """Sessions of one coach plan week keep their order and recovery days (same group)."""
    sequence_index: Mapped[int | None]
    preferred_day: Mapped[date | None] = mapped_column(Date)
    """The coach's suggested day (a soft preference, not a commitment)."""
    scheduling: Mapped[str] = mapped_column(
        String(10), default=_default_scheduling, server_default="flexible"
    )
    """Races are always fixed (database check); other kinds default to flexible."""
    all_day: Mapped[bool] = mapped_column(default=False, server_default="false")
    busy: Mapped[bool] = mapped_column(default=True, server_default="true")
    """All-day events: busy ones block their days, informative ones block nothing."""
    remote: Mapped[bool] = mapped_column(default=False, server_default="false")
    """Remote / no-travel activity: never gets a journey."""
    priority: Mapped[int] = mapped_column(default=2, server_default="2")
    series_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recurrence_series.id", ondelete="SET NULL")
    )
    occurrence_key: Mapped[str | None] = mapped_column(String(40))
    """Original start (UTC ISO) of a series occurrence: stable identity across edits."""
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("study_task.id", ondelete="CASCADE")
    )


class RecurrenceSeriesRow(Base):
    """RRULE series of personal activities; occurrences are materialised on a bounded horizon."""

    __tablename__ = "recurrence_series"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    rrule: Mapped[str] = mapped_column(String(500))
    dtstart: Mapped[datetime]
    """First occurrence start; expanded in ``timezone`` wall-clock time (DST-safe)."""
    timezone: Mapped[str] = mapped_column(String(64))
    template: Mapped[dict[str, Any]] = mapped_column(default=dict)
    """Fields copied to each occurrence (title, kind, scheduling, duration, place...)."""
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    ended_at: Mapped[datetime | None]
    """Set when the whole series is cancelled (rows kept for history)."""


class RecurrenceExceptionRow(Base):
    __tablename__ = "recurrence_exception"
    __table_args__ = (UniqueConstraint("series_id", "occurrence_key", name="exception_occurrence"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    series_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recurrence_series.id", ondelete="CASCADE")
    )
    occurrence_key: Mapped[str] = mapped_column(String(40))
    cancelled: Mapped[bool] = mapped_column(default=False, server_default="false")
    overrides: Mapped[dict[str, Any]] = mapped_column(default=dict)


class AvailabilityExceptionRow(Base):
    """Dated exception to the weekly availability: extra time (``available``) or blocked time."""

    __tablename__ = "availability_exception"
    __table_args__ = (
        CheckConstraint("end_minute > start_minute", name="positive_duration"),
        CheckConstraint("start_minute >= 0 AND end_minute <= 1440", name="minute_range"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    day: Mapped[date] = mapped_column(Date)
    start_minute: Mapped[int]
    end_minute: Mapped[int]
    available: Mapped[bool]
    place_id: Mapped[str | None] = mapped_column(ForeignKey("place.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")


class ReminderRow(Base):
    """User reminder: at a fixed time, or ``offset_minutes`` before an activity/event start.

    Relative reminders follow their target: the due time is recomputed from the target's
    current start at each check, so a moved activity never leaves a stale reminder."""

    __tablename__ = "reminder"
    __table_args__ = (
        CheckConstraint("target_type IN ('time', 'activity', 'event')", name="target_type"),
        CheckConstraint("status IN ('active', 'cancelled', 'done')", name="status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    message: Mapped[str] = mapped_column(String(300))
    target_type: Mapped[str] = mapped_column(String(10))
    target_id: Mapped[str | None] = mapped_column(String(64))
    offset_minutes: Mapped[int] = mapped_column(default=0, server_default="0")
    remind_at: Mapped[datetime | None]
    """Absolute time for ``target_type = 'time'`` (first occurrence when recurring)."""
    rrule: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(10), default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ConversationTurnRow(Base):
    """Recent conversation history (context for references such as "déplace-le")."""

    __tablename__ = "conversation_turn"
    __table_args__ = (Index("ix_conversation_turn_conv", "conversation_id", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]


class ProposalRow(Base):
    """Change to a protected commitment awaiting the user's approval in Home Assistant."""

    __tablename__ = "proposal"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected', 'obsolete')", name="status"),
        Index("ix_proposal_pending", "created_at", postgresql_where="status = 'pending'"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    created_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    input_revision: Mapped[str | None] = mapped_column(String(64))
    """Digest of the inputs the proposal was computed on; approval re-checks it (obsolete)."""


class NotificationRow(Base):
    __tablename__ = "notification"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True)
    created_at: Mapped[datetime]
    acknowledged_at: Mapped[datetime | None]


class SettingRow(Base):
    """Small key/value configuration set from Home Assistant (home place, tracker, prefs)."""

    __tablename__ = "app_setting"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)


class JobRunRow(Base):
    __tablename__ = "job_run"
    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    last_started_at: Mapped[datetime | None]
    last_finished_at: Mapped[datetime | None]
    last_status: Mapped[str] = mapped_column(String(20), default="never", server_default="never")
    last_error: Mapped[str | None] = mapped_column(Text)
