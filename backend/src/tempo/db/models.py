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
    __tablename__ = "study_task"
    __table_args__ = (
        CheckConstraint("effort_minutes > 0", name="positive_effort"),
        CheckConstraint("completed_minutes >= 0", name="non_negative_completed"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subject: Mapped[str] = mapped_column(String(200))
    effort_minutes: Mapped[int]
    completed_minutes: Mapped[int] = mapped_column(default=0, server_default="0")
    target_date: Mapped[date] = mapped_column(Date)
    target_kind: Mapped[TargetKind] = mapped_column(
        _enum(TargetKind, "target_kind"), default=TargetKind.SOFT, server_default="soft"
    )
    archived: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


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


class ActivityRow(Base):
    """Training (flexible: day/time movable, content and duration kept) or race (fixed)."""

    __tablename__ = "activity"
    __table_args__ = (
        CheckConstraint("kind IN ('training', 'race')", name="kind"),
        CheckConstraint("duration_minutes > 0", name="positive_duration"),
        CheckConstraint("latest_end_at > earliest_start_at", name="window"),
        UniqueConstraint("source", "source_ref", name="source_ref"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(300))
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    """Prescribed content, preserved verbatim (e.g. from the coach's e-mail)."""
    duration_minutes: Mapped[int]
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
