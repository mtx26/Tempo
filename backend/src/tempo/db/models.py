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
