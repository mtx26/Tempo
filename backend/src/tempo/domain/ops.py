"""Domain operations shared by the API, the conversational assistant and the worker.

One implementation per business rule (no duplicate logic between HTTP routes and AI tools):

- work log: completed work is the SUM of ``work_log`` entries (idempotent by key; a correction
  is a negative entry); ``study_task.completed_minutes`` is only a cache of that sum;
- task sessions: a task that must not be split is planned as ONE flexible activity (kind
  ``task``); a splittable task with a place as several located sessions (each with its
  journeys); other tasks as study blocks. Splitting depends on the task, never on its place;
- closing a task (completed or abandoned): every remaining planned session is cancelled,
  locked ones included; history (done sessions, work log) is kept;
- recurrence: RRULE series (python-dateutil) materialised as activity rows on a bounded
  horizon, wall-clock times in the series' timezone (DST-safe); exceptions keep the original
  occurrence key; editing one occurrence, the following ones or the whole series.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tempo.const import (
    MAX_SERIES_OCCURRENCES,
    MIN_SESSION_MINUTES,
    SERIES_HORIZON,
    SESSION_MINUTES,
    TASK_SESSION_HORIZON,
)
from tempo.db.models import (
    ActivityRow,
    RecurrenceExceptionRow,
    RecurrenceSeriesRow,
    StudyBlockRow,
    StudyTaskRow,
    WorkLogRow,
)
from tempo.timeutil import end_of_day

ACTIVE_STATES = ("active", "to_confirm")


class DomainError(ValueError):
    """A request that breaks a business rule; the message is safe to show to the user."""


# --- work log ------------------------------------------------------------------------------------


def record_work(
    session: Session,
    task: StudyTaskRow,
    minutes: int,
    *,
    now: datetime,
    tz: ZoneInfo,
    block_id: uuid.UUID | None = None,
    key: str | None = None,
    note: str = "",
) -> bool:
    """Add a work-log entry (negative = correction) and recompute the task's done minutes.

    Returns False when ``key`` was already recorded (idempotent retry: nothing changes)."""
    if minutes == 0:
        raise DomainError("durée de travail nulle")
    done = _logged(session, task.id)
    if done + minutes < 0:
        raise DomainError(f"correction impossible : seulement {done} min enregistrées")
    statement = insert(WorkLogRow).values(
        id=uuid.uuid4(),
        task_id=task.id,
        block_id=block_id,
        minutes=minutes,
        recorded_at=now,
        note=note[:500],
        idempotency_key=key,
    )
    if key is not None:
        statement = statement.on_conflict_do_nothing(index_elements=["idempotency_key"])
    inserted = session.execute(statement.returning(WorkLogRow.id)).first() is not None
    task.completed_minutes = _logged(session, task.id)
    if task.effort_minutes is not None and task.completed_minutes >= task.effort_minutes:
        close_task(session, task, now, tz)
        return inserted
    if task.archived and task.effort_minutes is not None:
        task.archived = False  # a correction re-opens a task marked complete too early
    sync_task_session(session, task, now, tz)
    return inserted


def close_task(session: Session, task: StudyTaskRow, now: datetime, tz: ZoneInfo) -> int:
    """The task is completed (or abandoned): cancel every remaining planned study block,
    locked ones included, and its sessions; never reported as missed. Returns the number of
    cancelled blocks."""
    task.archived = True
    blocks = list(
        session.scalars(
            select(StudyBlockRow).where(
                StudyBlockRow.task_id == task.id, StudyBlockRow.state == "planned"
            )
        )
    )
    for block in blocks:
        block.state = "cancelled"
    sync_task_session(session, task, now, tz)
    return len(blocks)


def _logged(session: Session, task_id: uuid.UUID) -> int:
    total = session.scalar(
        select(func.coalesce(func.sum(WorkLogRow.minutes), 0)).where(WorkLogRow.task_id == task_id)
    )
    return int(total or 0)


def remaining_minutes(task: StudyTaskRow) -> int | None:
    if task.effort_minutes is None:
        return None
    return max(0, task.effort_minutes - task.completed_minutes)


def complete_block(
    session: Session, block: StudyBlockRow, now: datetime, tz: ZoneInfo, minutes: int | None
) -> int:
    """A study block reported done (fully, or ``minutes`` of it): logged once per block."""
    length = int((block.end_at - block.start_at).total_seconds() // 60)
    if minutes is not None and not 0 < minutes <= length:
        raise DomainError(f"durée invalide : entre 1 et {length} min pour cette séance")
    worked = length if minutes is None else minutes
    task = session.get(StudyTaskRow, block.task_id)
    if task is None:
        raise DomainError("tâche introuvable")
    block.state = "done" if worked >= length else "partial"
    record_work(session, task, worked, now=now, tz=tz, block_id=block.id, key=f"block:{block.id}")
    return worked


# --- task sessions -------------------------------------------------------------------------------


def needs_session(task: StudyTaskRow) -> bool:
    """Planned as activities (sessions) rather than study blocks: non-splittable work, or work
    at a given place (each located session gets its journeys)."""
    return task.place_id is not None or not task.splittable


def session_lengths(remaining: int) -> list[int]:
    """Sessions of a splittable located task: ``SESSION_MINUTES`` each, the last one at
    least ``MIN_SESSION_MINUTES``."""
    count = -(-remaining // SESSION_MINUTES)
    lengths = [SESSION_MINUTES] * (count - 1) + [remaining - SESSION_MINUTES * (count - 1)]
    if count > 1 and lengths[-1] < MIN_SESSION_MINUTES:
        lengths[-2] -= MIN_SESSION_MINUTES - lengths[-1]
        lengths[-1] = MIN_SESSION_MINUTES
    return lengths


def sync_task_session(
    session: Session, task: StudyTaskRow, now: datetime, tz: ZoneInfo
) -> list[ActivityRow]:
    """Keep the session activities of a task in line with it (remaining work, place, window).

    Window: from now to the local end of the deadline day (soft or strict: work is never
    planned after a deadline without the user's approval); without deadline, the planning
    horizon (no artificial deadline). A non-splittable task is one session; a splittable
    located one is several, created for the work due within ``TASK_SESSION_HORIZON`` (regular
    distribution, never everything at once)."""
    current = list(
        session.scalars(
            select(ActivityRow)
            .where(ActivityRow.task_id == task.id, ActivityRow.status.in_(ACTIVE_STATES))
            .order_by(ActivityRow.sequence_index, ActivityRow.created_at)
        )
    )
    remaining = remaining_minutes(task)
    if task.archived or not needs_session(task) or not remaining:
        for row in current:
            row.status = "cancelled"
        return []
    start = now
    if task.target_date is not None:
        end = end_of_day(task.target_date, tz)
        if end <= start:
            start = end - timedelta(minutes=remaining)  # passed: kept visible, reported
    else:
        end = now + TASK_SESSION_HORIZON
    if not task.splittable:
        lengths = [remaining]
    else:
        due = remaining
        if task.target_date is not None and end - now > TASK_SESSION_HORIZON:
            share = remaining * TASK_SESSION_HORIZON / (end - now)
            due = min(remaining, max(MIN_SESSION_MINUTES, -(-int(share) // 15) * 15))
        lengths = session_lengths(due)
    rows: list[ActivityRow] = []
    for index, minutes in enumerate(lengths):
        if index < len(current):
            row = current[index]
        else:
            row = ActivityRow(
                kind="task", source="task", task_id=task.id, scheduling="flexible", status="active"
            )
            session.add(row)
        row.title = task.subject
        row.notes = task.notes
        row.duration_minutes = minutes
        row.earliest_start_at = min(start, end - timedelta(minutes=minutes))
        row.latest_end_at = max(end, row.earliest_start_at + timedelta(minutes=minutes))
        row.place_id = task.place_id
        row.priority = task.priority
        row.sequence_group = f"task:{task.id}" if len(lengths) > 1 else None
        row.sequence_index = index
        rows.append(row)
    for row in current[len(lengths) :]:
        row.status = "cancelled"
    session.flush()
    return rows


def refresh_task_sessions(session: Session, now: datetime, tz: ZoneInfo) -> None:
    """Re-derive every task session window from its task (run before each re-plan, so a
    window computed days ago never silently expires)."""
    for task in session.scalars(select(StudyTaskRow).where(StudyTaskRow.archived.is_(False))):
        if needs_session(task):
            sync_task_session(session, task, now, tz)


# --- recurrence ----------------------------------------------------------------------------------

TEMPLATE_FIELDS = frozenset(
    {
        "title",
        "kind",
        "scheduling",
        "duration_minutes",
        "window_minutes",
        "place_id",
        "notes",
        "all_day",
        "busy",
        "remote",
        "priority",
    }
)


@dataclass(frozen=True, slots=True)
class Occurrence:
    key: str
    start: datetime


def occurrence_key(local_start: datetime) -> str:
    return local_start.replace(tzinfo=None).isoformat(timespec="minutes")


def occurrences(series: RecurrenceSeriesRow, start: datetime, end: datetime) -> list[Occurrence]:
    """Occurrences of the series starting in ``[start, end)`` (wall-clock expansion)."""
    tz = ZoneInfo(series.timezone)
    local_start = series.dtstart.astimezone(tz).replace(tzinfo=None)
    try:
        rule = rrulestr(series.rrule, dtstart=local_start)
    except (ValueError, TypeError) as exc:
        raise DomainError(f"règle de récurrence invalide : {exc}") from exc
    found: list[Occurrence] = []
    lower = start.astimezone(tz).replace(tzinfo=None) - timedelta(days=1)
    upper = end.astimezone(tz).replace(tzinfo=None)
    for naive in rule.xafter(lower, inc=True, count=MAX_SERIES_OCCURRENCES):
        if naive >= upper:
            break
        aware = naive.replace(tzinfo=tz)
        # A wall-clock time that does not exist (DST gap) is shifted by fold handling of
        # zoneinfo; comparing in UTC keeps the window exact.
        if aware.astimezone(UTC) < start.astimezone(UTC):
            continue
        found.append(Occurrence(occurrence_key(naive), aware))
    return found


def validate_rrule(rule: str) -> str:
    text = rule.strip().removeprefix("RRULE:")
    if not text or len(text) > 400 or "\n" in text:
        raise DomainError("règle de récurrence invalide")
    try:
        rrulestr(text, dtstart=datetime(2026, 1, 1))  # noqa: DTZ001 - naive wall-clock by design
    except (ValueError, TypeError) as exc:
        raise DomainError(f"règle de récurrence invalide : {exc}") from exc
    if "FREQ=SECONDLY" in text or "FREQ=MINUTELY" in text or "FREQ=HOURLY" in text:
        raise DomainError("récurrence trop fréquente (au plus quotidienne)")
    return text


def _apply_template(row: ActivityRow, template: dict[str, Any], start: datetime) -> None:
    duration = int(template.get("duration_minutes") or 0)
    if duration <= 0:
        raise DomainError("durée de l'occurrence inconnue")
    window = max(duration, int(template.get("window_minutes") or duration))
    row.kind = template.get("kind", "personal")
    row.title = str(template.get("title", "Événement"))[:300]
    row.notes = str(template.get("notes", ""))
    row.scheduling = template.get("scheduling", "fixed")
    row.duration_minutes = duration
    row.earliest_start_at = start
    row.latest_end_at = start + timedelta(minutes=duration if row.scheduling == "fixed" else window)
    row.place_id = template.get("place_id")
    row.all_day = bool(template.get("all_day", False))
    row.busy = bool(template.get("busy", True))
    row.remote = bool(template.get("remote", False))
    row.priority = int(template.get("priority", 2))


def expand_series(session: Session, series: RecurrenceSeriesRow, now: datetime) -> int:
    """Materialise missing occurrences up to ``now + SERIES_HORIZON``. Existing rows (possibly
    edited by the user) are never overwritten. Returns the number of rows created."""
    if series.ended_at is not None:
        return 0
    existing = set(
        session.scalars(
            select(ActivityRow.occurrence_key).where(ActivityRow.series_id == series.id)
        )
    )
    exceptions = {
        e.occurrence_key: e
        for e in session.scalars(
            select(RecurrenceExceptionRow).where(RecurrenceExceptionRow.series_id == series.id)
        )
    }
    created = 0
    for occurrence in occurrences(series, max(series.dtstart, now), now + SERIES_HORIZON):
        if occurrence.key in existing:
            continue
        exception = exceptions.get(occurrence.key)
        if exception is not None and exception.cancelled:
            continue
        template = dict(series.template)
        start = occurrence.start
        if exception is not None:
            overrides = exception.overrides
            template.update({k: v for k, v in overrides.items() if k in TEMPLATE_FIELDS})
            if "start" in overrides:
                start = datetime.fromisoformat(overrides["start"])
        row = ActivityRow(
            series_id=series.id,
            occurrence_key=occurrence.key,
            source="series",
            status="active",
        )
        _apply_template(row, template, start)
        session.add(row)
        created += 1
    session.flush()
    return created


def create_series(
    session: Session,
    *,
    rule: str,
    first_start: datetime,
    tz: ZoneInfo,
    template: dict[str, Any],
    now: datetime,
) -> RecurrenceSeriesRow:
    unknown = set(template) - TEMPLATE_FIELDS
    if unknown:
        raise DomainError(f"champs inconnus : {', '.join(sorted(unknown))}")
    series = RecurrenceSeriesRow(
        rrule=validate_rrule(rule),
        dtstart=first_start,
        timezone=str(tz),
        template=template,
    )
    session.add(series)
    session.flush()
    expand_series(session, series, now)
    return series


def _future_rows(
    session: Session, series: RecurrenceSeriesRow, from_key: str | None
) -> list[ActivityRow]:
    rows = session.scalars(
        select(ActivityRow).where(
            ActivityRow.series_id == series.id, ActivityRow.status.in_(ACTIVE_STATES)
        )
    )
    return [r for r in rows if from_key is None or (r.occurrence_key or "") >= from_key]


def edit_occurrence(
    session: Session,
    row: ActivityRow,
    *,
    cancel: bool = False,
    overrides: dict[str, Any] | None = None,
) -> None:
    """Change or cancel ONE occurrence; recorded as an exception so re-expansion keeps it."""
    if row.series_id is None or row.occurrence_key is None:
        raise DomainError("cette activité ne fait pas partie d'une série")
    values = dict(overrides or {})
    statement = insert(RecurrenceExceptionRow).values(
        id=uuid.uuid4(),
        series_id=row.series_id,
        occurrence_key=row.occurrence_key,
        cancelled=cancel,
        overrides=values,
    )
    session.execute(
        statement.on_conflict_do_update(
            constraint="exception_occurrence",
            set_={"cancelled": cancel, "overrides": values},
        )
    )
    if cancel:
        row.status = "cancelled"


def end_series_before(
    session: Session, series: RecurrenceSeriesRow, from_key: str, now: datetime
) -> None:
    """Stop the series before occurrence ``from_key`` (scope "this and following")."""
    tz = ZoneInfo(series.timezone)
    cut = datetime.fromisoformat(from_key).replace(tzinfo=tz) - timedelta(seconds=1)
    parts = [p for p in series.rrule.split(";") if not p.upper().startswith(("UNTIL=", "COUNT="))]
    parts.append("UNTIL=" + cut.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"))
    series.rrule = ";".join(parts)
    for row in _future_rows(session, series, from_key):
        if row.status != "done":
            row.status = "cancelled"
    if cut <= now:
        series.ended_at = now


def cancel_series(session: Session, series: RecurrenceSeriesRow, now: datetime) -> int:
    rows = [r for r in _future_rows(session, series, None) if r.earliest_start_at >= now]
    for row in rows:
        row.status = "cancelled"
    series.ended_at = now
    return len(rows)


def update_series(
    session: Session, series: RecurrenceSeriesRow, changes: dict[str, Any], now: datetime
) -> int:
    """Change the whole series: future occurrences without their own exception are rebuilt."""
    unknown = set(changes) - TEMPLATE_FIELDS
    if unknown:
        raise DomainError(f"champs inconnus : {', '.join(sorted(unknown))}")
    series.template = {**series.template, **changes}
    excepted = set(
        session.scalars(
            select(RecurrenceExceptionRow.occurrence_key).where(
                RecurrenceExceptionRow.series_id == series.id
            )
        )
    )
    updated = 0
    for row in _future_rows(session, series, None):
        if row.earliest_start_at < now or row.locked or row.occurrence_key in excepted:
            continue
        _apply_template(row, series.template, row.earliest_start_at)
        updated += 1
    session.flush()
    return updated
