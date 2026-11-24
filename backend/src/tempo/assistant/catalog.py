"""Catalogue of business operations: the ONLY mutation surface of Tempo.

The same typed operations are exposed to the conversational assistant (LLM tool use) and to
authenticated API clients (``GET /v1/operations``, ``POST /v1/operations/{name}``). Each has a
Pydantic input schema (validated before running), a typed JSON result and an explicit
``mutates`` flag. Business rules live in the handlers / ``tempo.domain.ops``; an operation
never bypasses them, and nothing here gives access to SQL, the shell, Docker or secrets.

Times: an input datetime without offset is local time in the configured timezone
(Europe/Brussels). Results always carry ISO 8601 with offset.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tempo.db.models import (
    ActivityRow,
    AuditEntry,
    AvailabilityExceptionRow,
    AvailabilityRow,
    CategoryWindowRow,
    FixedEventRow,
    JobRunRow,
    NotificationRow,
    PhonePositionRow,
    PlaceAliasRow,
    PlaceRow,
    ProposalRow,
    RecurrenceSeriesRow,
    ReminderRow,
    ScheduleVersion,
    StudyBlockRow,
    StudyTaskRow,
    SyncRun,
)
from tempo.domain.models import TargetKind
from tempo.domain.ops import (
    DomainError,
    cancel_series,
    complete_block,
    create_series,
    edit_occurrence,
    end_series_before,
    expand_series,
    record_work,
    remaining_minutes,
    sync_task_session,
    update_series,
)
from tempo.planning.proposals import decide, proposal_out
from tempo.planning.service import (
    PlanningNotConfiguredError,
    active_version,
    audit,
    get_setting,
    replan,
    set_setting,
)
from tempo.planning.views import schedule_items, summary
from tempo.scheduler.model import Preferences
from tempo.transport.places import Coordinates
from tempo.transport.routing import Journey, RoutingProvider, RoutingUnavailableError

MAX_SPAN = timedelta(days=62)
POSITION_MAX_AGE = timedelta(minutes=10)
POSITION_MAX_ACCURACY_M = 200.0


@dataclass(slots=True)
class OpContext:
    session: Session
    now: datetime
    tz: ZoneInfo
    actor: str
    routing: RoutingProvider | None = None
    changed: list[str] = field(default_factory=list)
    """Mutating operations run in this context (for the re-plan request and the audit)."""


class OpError(Exception):
    """Safe, user-facing error of an operation (validation or business rule)."""


@dataclass(frozen=True, slots=True)
class Operation:
    name: str
    description: str
    model: type[BaseModel]
    handler: Callable[[OpContext, Any], dict[str, Any]]
    mutates: bool
    replan: bool = False
    """A successful run invalidates the plan: a re-plan is requested."""


CATALOG: dict[str, Operation] = {}


def operation(
    name: str, description: str, model: type[BaseModel], *, mutates: bool, replan: bool = False
) -> Callable[
    [Callable[[OpContext, Any], dict[str, Any]]], Callable[[OpContext, Any], dict[str, Any]]
]:
    def register(
        handler: Callable[[OpContext, Any], dict[str, Any]],
    ) -> Callable[[OpContext, Any], dict[str, Any]]:
        CATALOG[name] = Operation(name, description, model, handler, mutates, replan)
        return handler

    return register


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoInput(Strict):
    pass


def _local(value: datetime, tz: ZoneInfo) -> datetime:
    return value.replace(tzinfo=tz) if value.tzinfo is None else value


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value.removeprefix(f"{what}:"))
    except ValueError as exc:
        raise OpError(f"identifiant {what} invalide : {value}") from exc


def _place_exists(ctx: OpContext, place_id: str | None) -> None:
    if place_id is not None and ctx.session.get(PlaceRow, place_id) is None:
        raise OpError(f"lieu inconnu : {place_id} (créez-le d'abord avec upsert_place)")


# --- reading ---


@operation(
    "get_context",
    "Current date/time and timezone, configured home place and phone tracker, planning "
    "settings, planned locations. Call first when dates or places are involved.",
    NoInput,
    mutates=False,
)
def get_context(ctx: OpContext, _: NoInput) -> dict[str, Any]:
    local = ctx.now.astimezone(ctx.tz)
    return {
        "now": local.isoformat(timespec="minutes"),
        "weekday": local.strftime("%A"),
        "timezone": str(ctx.tz),
        "home_place_id": get_setting(ctx.session, "home_place_id"),
        "tracker_entity_id": get_setting(ctx.session, "tracker_entity_id"),
        "horizon_days": get_setting(ctx.session, "horizon_days", 14),
        "buffer_minutes": get_setting(ctx.session, "buffer_minutes", 10),
        "departure_reminder_minutes": get_setting(ctx.session, "reminder_minutes", [60, 15]),
        "study_preferences": get_setting(ctx.session, "study_preferences", {}) or {},
        "planned_locations": get_setting(ctx.session, "stays", []) or [],
    }


class RangeIn(Strict):
    start: datetime = Field(description="Range start (local time if no offset)")
    end: datetime = Field(description="Range end, at most 62 days after start")


def _range(ctx: OpContext, body: RangeIn) -> tuple[datetime, datetime]:
    start, end = _local(body.start, ctx.tz), _local(body.end, ctx.tz)
    if end <= start or end - start > MAX_SPAN:
        raise OpError("intervalle invalide (au plus 62 jours)")
    return start, end


@operation(
    "get_schedule",
    "Published plan over a range: university classes (fixed), personal events, trainings, "
    "races, task sessions, study blocks and journeys, sorted by start.",
    RangeIn,
    mutates=False,
)
def get_schedule(ctx: OpContext, body: RangeIn) -> dict[str, Any]:
    return {"items": schedule_items(ctx.session, *_range(ctx, body))}


@operation(
    "get_summary",
    "Short state: next item, today's study, remaining work, pending proposals, unread "
    "notifications, plan status and next departure check.",
    NoInput,
    mutates=False,
)
def get_summary(ctx: OpContext, _: NoInput) -> dict[str, Any]:
    return summary(ctx.session, ctx.now, ctx.tz)


class ActivitiesIn(Strict):
    start: datetime | None = None
    end: datetime | None = None
    kind: Literal["personal", "training", "race", "task"] | None = None
    text: str | None = Field(default=None, max_length=200, description="Title contains")
    include_inactive: bool = False


def activity_out(row: ActivityRow) -> dict[str, Any]:
    begins = row.earliest_start_at if row.scheduling == "fixed" else row.planned_start_at
    return {
        "id": str(row.id),
        "kind": row.kind,
        "title": row.title,
        "notes": row.notes,
        "scheduling": row.scheduling,
        "status": row.status,
        "locked": row.locked,
        "all_day": row.all_day,
        "busy": row.busy,
        "remote": row.remote,
        "priority": row.priority,
        "duration_minutes": row.duration_minutes,
        "window_start": _iso(row.earliest_start_at),
        "window_end": _iso(row.latest_end_at),
        "planned_start": _iso(begins),
        "preferred_start": _iso(row.preferred_start_at),
        "preferred_day": None if row.preferred_day is None else row.preferred_day.isoformat(),
        "place_id": row.place_id,
        "series_id": None if row.series_id is None else str(row.series_id),
        "occurrence_key": row.occurrence_key,
        "task_id": None if row.task_id is None else str(row.task_id),
        "source": row.source,
    }


@operation(
    "list_activities",
    "Search activities (personal events, trainings, races, task sessions) by period, kind or "
    "title. Returns ids needed to modify them.",
    ActivitiesIn,
    mutates=False,
)
def list_activities(ctx: OpContext, body: ActivitiesIn) -> dict[str, Any]:
    query = select(ActivityRow).order_by(ActivityRow.earliest_start_at)
    if not body.include_inactive:
        query = query.where(ActivityRow.status.in_(("active", "to_confirm")))
    if body.kind:
        query = query.where(ActivityRow.kind == body.kind)
    if body.start:
        query = query.where(ActivityRow.latest_end_at > _local(body.start, ctx.tz))
    if body.end:
        query = query.where(ActivityRow.earliest_start_at < _local(body.end, ctx.tz))
    if body.text:
        query = query.where(ActivityRow.title.ilike(f"%{body.text}%"))
    rows = list(ctx.session.scalars(query.limit(100)))
    return {"activities": [activity_out(r) for r in rows]}


class TasksIn(Strict):
    include_archived: bool = False


def task_out(row: StudyTaskRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "title": row.subject,
        "estimated_minutes": row.effort_minutes,
        "done_minutes": row.completed_minutes,
        "remaining_minutes": remaining_minutes(row),
        "target_date": None if row.target_date is None else row.target_date.isoformat(),
        "hard_deadline": row.target_kind is TargetKind.HARD,
        "priority": row.priority,
        "splittable": row.splittable,
        "place_id": row.place_id,
        "notes": row.notes,
        "archived": row.archived,
        "plannable": row.effort_minutes is not None,
    }


@operation(
    "list_tasks",
    "Tasks and study subjects with estimated, done and remaining minutes. A task without "
    "estimated duration is kept but not planned.",
    TasksIn,
    mutates=False,
)
def list_tasks(ctx: OpContext, body: TasksIn) -> dict[str, Any]:
    query = select(StudyTaskRow).order_by(StudyTaskRow.created_at)
    if not body.include_archived:
        query = query.where(StudyTaskRow.archived.is_(False))
    return {"tasks": [task_out(r) for r in ctx.session.scalars(query)]}


@operation("list_places", "Known places with coordinates and aliases.", NoInput, mutates=False)
def list_places(ctx: OpContext, _: NoInput) -> dict[str, Any]:
    aliases: dict[str, list[str]] = {}
    for alias in ctx.session.scalars(select(PlaceAliasRow)):
        aliases.setdefault(alias.place_id, []).append(alias.alias)
    return {
        "places": [
            {
                "id": p.id,
                "name": p.name,
                "latitude": p.latitude,
                "longitude": p.longitude,
                "aliases": sorted(aliases.get(p.id, [])),
            }
            for p in ctx.session.scalars(select(PlaceRow).order_by(PlaceRow.id))
        ]
    }


@operation(
    "get_position",
    "Latest REAL phone GPS position pushed by Home Assistant, its age and whether it is "
    "fresh and accurate enough to be a travel origin now. Never replaced by plans.",
    NoInput,
    mutates=False,
)
def get_position(ctx: OpContext, _: NoInput) -> dict[str, Any]:
    tracker = get_setting(ctx.session, "tracker_entity_id")
    row = ctx.session.get(PhonePositionRow, tracker) if tracker else None
    if row is None:
        return {"available": False, "reason": "aucune position reçue de Home Assistant"}
    age = ctx.now - row.observed_at
    reliable = age <= POSITION_MAX_AGE and row.accuracy_m <= POSITION_MAX_ACCURACY_M
    return {
        "available": True,
        "latitude": row.latitude,
        "longitude": row.longitude,
        "accuracy_m": row.accuracy_m,
        "observed_at": _iso(row.observed_at),
        "age_minutes": int(age.total_seconds() // 60),
        "reliable_now": reliable,
    }


def _minutes_text(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


@operation(
    "get_rules",
    "Availability rules (weekly windows by place), category windows of flexible activities, "
    "dated exceptions, planned locations "
    "(where the user will be), study preferences and reminder settings.",
    NoInput,
    mutates=False,
)
def get_rules(ctx: OpContext, _: NoInput) -> dict[str, Any]:
    windows = ctx.session.scalars(
        select(AvailabilityRow).order_by(AvailabilityRow.weekday, AvailabilityRow.start_minute)
    )
    exceptions = ctx.session.scalars(
        select(AvailabilityExceptionRow)
        .where(AvailabilityExceptionRow.day >= ctx.now.astimezone(ctx.tz).date())
        .order_by(AvailabilityExceptionRow.day)
    )
    return {
        "weekly_windows": [
            {
                "weekday": w.weekday,
                "start": _minutes_text(w.start_minute),
                "end": _minutes_text(w.end_minute),
                "place_id": w.place_id,
            }
            for w in windows
        ],
        "exceptions": [
            {
                "id": str(e.id),
                "day": e.day.isoformat(),
                "start": _minutes_text(e.start_minute),
                "end": _minutes_text(e.end_minute),
                "available": e.available,
                "place_id": e.place_id,
                "reason": e.reason,
            }
            for e in exceptions
        ],
        "category_windows": _category_windows(ctx.session),
        "planned_locations": get_setting(ctx.session, "stays", []) or [],
        "study_preferences": get_setting(ctx.session, "study_preferences", {}) or {},
        "departure_reminder_minutes": get_setting(ctx.session, "reminder_minutes", [60, 15]),
    }


class ProposalsIn(Strict):
    status: Literal["pending", "approved", "rejected", "obsolete"] = "pending"


@operation(
    "list_proposals",
    "Changes to protected commitments awaiting the user's natural-language approval.",
    ProposalsIn,
    mutates=False,
)
def list_proposals(ctx: OpContext, body: ProposalsIn) -> dict[str, Any]:
    rows = ctx.session.scalars(
        select(ProposalRow)
        .where(ProposalRow.status == body.status)
        .order_by(ProposalRow.created_at)
    )
    return {"proposals": [proposal_out(r) for r in rows]}


class NotificationsIn(Strict):
    unread_only: bool = True
    limit: int = Field(default=20, ge=1, le=100)


@operation(
    "list_notifications",
    "Recent notifications (alerts, conflicts, reminders).",
    NotificationsIn,
    mutates=False,
)
def list_notifications(ctx: OpContext, body: NotificationsIn) -> dict[str, Any]:
    query = select(NotificationRow).order_by(NotificationRow.created_at.desc()).limit(body.limit)
    if body.unread_only:
        query = query.where(NotificationRow.acknowledged_at.is_(None))
    return {
        "notifications": [
            {
                "id": str(n.id),
                "kind": n.kind,
                "title": n.title,
                "message": n.message,
                "created_at": _iso(n.created_at),
            }
            for n in ctx.session.scalars(query)
        ]
    }


def reminder_out(row: ReminderRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "message": row.message,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "offset_minutes": row.offset_minutes,
        "at": _iso(row.remind_at),
        "rrule": row.rrule,
        "status": row.status,
    }


@operation("list_reminders", "Active user reminders.", NoInput, mutates=False)
def list_reminders(ctx: OpContext, _: NoInput) -> dict[str, Any]:
    rows = ctx.session.scalars(
        select(ReminderRow).where(ReminderRow.status == "active").order_by(ReminderRow.created_at)
    )
    return {"reminders": [reminder_out(r) for r in rows]}


@operation(
    "explain_plan",
    "Why the plan is what it is: solver status, completeness, unplanned and late work, "
    "violated strict deadlines, conflicts, unplaced activities, travel issues, what changed in "
    "the last re-plan, and the last rejected attempt. Use it to explain, never invent.",
    NoInput,
    mutates=False,
)
def explain_plan(ctx: OpContext, _: NoInput) -> dict[str, Any]:
    version = active_version(ctx.session)
    rejected = ctx.session.scalars(
        select(ScheduleVersion)
        .where(ScheduleVersion.state == "rejected")
        .order_by(ScheduleVersion.created_at.desc())
    ).first()
    subjects = {str(t.id): t.subject for t in ctx.session.scalars(select(StudyTaskRow))}
    titles = {str(a.id): a.title for a in ctx.session.scalars(select(ActivityRow))}

    def described(plan: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in plan.items() if k != "travel"}
        out["unplanned_minutes"] = {
            subjects.get(k, k): v for k, v in plan.get("unplanned_minutes", {}).items()
        }
        out["late_minutes"] = {
            subjects.get(k, k): v for k, v in plan.get("late_minutes", {}).items()
        }
        out["hard_deadline_misses"] = [
            subjects.get(k, k) for k in plan.get("hard_deadline_misses", [])
        ]
        out["deadline_capacity"] = {
            subjects.get(k, k): v for k, v in (plan.get("deadline_capacity") or {}).items()
        }
        out["unplaced_activities"] = [titles.get(k, k) for k in plan.get("unplaced_trainings", [])]
        out.pop("unplaced_trainings", None)
        out["journeys_with_problems"] = [
            t for t in plan.get("travel", []) if t.get("status") != "routed"
        ]
        return out

    return {
        "active_plan": None
        if version is None
        else {"solver_status": version.solver_status, **described(version.summary)},
        "last_rejected_attempt": None
        if rejected is None or (version is not None and rejected.created_at < version.created_at)
        else {"solver_status": rejected.solver_status, **described(rejected.summary)},
    }


@operation(
    "get_sources_status",
    "Health and freshness of automatic jobs (iCal sync, re-plan, departure checks, coach "
    "e-mails), last iCal sync result and latest errors.",
    NoInput,
    mutates=False,
)
def get_sources_status(ctx: OpContext, _: NoInput) -> dict[str, Any]:
    jobs = [
        {
            "name": j.name,
            "last_finished": _iso(j.last_finished_at),
            "status": j.last_status,
            "error": j.last_error,
        }
        for j in ctx.session.scalars(select(JobRunRow).order_by(JobRunRow.name))
    ]
    sync = ctx.session.scalars(select(SyncRun).order_by(SyncRun.started_at.desc())).first()
    return {
        "jobs": jobs,
        "last_ical_sync": None
        if sync is None
        else {
            "at": _iso(sync.started_at),
            "status": sync.status,
            "added": sync.added,
            "changed": sync.changed,
            "removed": sync.removed,
        },
        "departure_check": get_setting(ctx.session, "departure_check"),
        "llm_required": False,
    }


class HistoryIn(Strict):
    limit: int = Field(default=20, ge=1, le=100)


@operation(
    "get_history", "Recent audited changes (who changed what, when).", HistoryIn, mutates=False
)
def get_history(ctx: OpContext, body: HistoryIn) -> dict[str, Any]:
    rows = ctx.session.scalars(select(AuditEntry).order_by(AuditEntry.at.desc()).limit(body.limit))
    return {
        "entries": [
            {
                "at": _iso(r.at),
                "actor": r.actor,
                "action": r.action,
                "entity": r.entity,
                "id": r.entity_id,
            }
            for r in rows
        ]
    }


# --- events ---


class EventIn(Strict):
    title: str = Field(min_length=1, max_length=300)
    kind: Literal["personal", "training", "race"] = Field(
        default="personal",
        description="training = sport session (flexible by default, prescribed content in "
        "notes, kept verbatim); race = competition (always fixed)",
    )
    scheduling: Literal["fixed", "flexible"] = Field(
        description="fixed = imposed time (appointment); flexible = Tempo chooses the time "
        "inside [window_start, window_end]"
    )
    start: datetime | None = Field(default=None, description="Fixed start (or all-day first day)")
    end: datetime | None = Field(default=None, description="Fixed end (alternative to duration)")
    duration_minutes: int | None = Field(default=None, gt=0, le=14 * 24 * 60)
    window_start: datetime | None = None
    window_end: datetime | None = None
    preferred_start: datetime | None = Field(
        default=None, description="Flexible only: preferred start inside the window (soft)"
    )
    all_day: bool = False
    all_day_last_day: date | None = Field(default=None, description="Multi-day all-day event")
    busy: bool = Field(default=True, description="All-day events: False = informative only")
    place_id: str | None = None
    remote: bool = Field(default=False, description="No travel needed (online, at home)")
    locked: bool = False
    priority: int = Field(default=2, ge=1, le=3)
    notes: str = Field(default="", max_length=5000)
    rrule: str | None = Field(
        default=None,
        description="RFC 5545 RRULE for a recurring event, e.g. FREQ=WEEKLY;BYDAY=TU;COUNT=10",
    )


def _event_times(ctx: OpContext, body: EventIn) -> tuple[datetime, datetime, int]:
    if body.all_day:
        if body.start is None:
            raise OpError("un événement journée entière a besoin d'un jour (start)")
        first = _local(body.start, ctx.tz).astimezone(ctx.tz).date()
        last = body.all_day_last_day or first
        if last < first or (last - first).days > 31:
            raise OpError("dernier jour invalide")
        begin = datetime.combine(first, time(0), tzinfo=ctx.tz)
        finish = datetime.combine(last + timedelta(days=1), time(0), tzinfo=ctx.tz)
        return begin, finish, int((finish - begin).total_seconds() // 60)
    if body.scheduling == "fixed":
        if body.start is None or (body.end is None and body.duration_minutes is None):
            raise OpError("un événement fixe a besoin d'un début et d'une fin ou d'une durée")
        begin = _local(body.start, ctx.tz)
        finish = (
            _local(body.end, ctx.tz)
            if body.end is not None
            else begin + timedelta(minutes=body.duration_minutes or 0)
        )
        if finish <= begin:
            raise OpError("la fin doit être après le début")
        return begin, finish, int((finish - begin).total_seconds() // 60)
    if body.duration_minutes is None:
        raise OpError("durée inconnue : demandez-la à l'utilisateur (jamais inventée)")
    if body.window_start is None or body.window_end is None:
        raise OpError("un événement flexible a besoin d'une fenêtre (window_start, window_end)")
    begin, finish = _local(body.window_start, ctx.tz), _local(body.window_end, ctx.tz)
    if finish - begin < timedelta(minutes=body.duration_minutes):
        raise OpError("fenêtre plus courte que la durée")
    if finish - begin > timedelta(days=14):
        raise OpError("fenêtre de plus de 14 jours")
    return begin, finish, body.duration_minutes


@operation(
    "create_event",
    "Create an event: personal (fixed appointment, flexible outing where Tempo picks the time "
    "in a window, all-day busy or informative, multi-day, recurring RRULE), a training "
    "(flexible) or a race (fixed). Never invent a duration, time or place: ask when unknown.",
    EventIn,
    mutates=True,
    replan=True,
)
def create_event(ctx: OpContext, body: EventIn) -> dict[str, Any]:
    _place_exists(ctx, body.place_id)
    if body.kind == "race" and (body.scheduling != "fixed" or body.all_day):
        raise OpError("une compétition est toujours fixe : demandez son heure de départ")
    begin, finish, duration = _event_times(ctx, body)
    if finish <= ctx.now:
        raise OpError("l'événement est déjà passé")
    scheduling = "fixed" if body.all_day else body.scheduling
    preferred = None
    if body.preferred_start is not None:
        if scheduling != "flexible":
            raise OpError("heure préférée seulement pour un événement flexible")
        preferred = _local(body.preferred_start, ctx.tz)
        if not begin <= preferred <= finish - timedelta(minutes=duration):
            raise OpError("heure préférée hors de la fenêtre")
    template = {
        "title": body.title,
        "kind": body.kind,
        "scheduling": scheduling,
        "duration_minutes": duration,
        "window_minutes": int((finish - begin).total_seconds() // 60),
        "place_id": body.place_id,
        "notes": body.notes,
        "all_day": body.all_day,
        "busy": body.busy,
        "remote": body.remote,
        "priority": body.priority,
    }
    if body.rrule:
        try:
            series = create_series(
                ctx.session,
                rule=body.rrule,
                first_start=begin,
                tz=ctx.tz,
                template=template,
                now=ctx.now,
            )
        except DomainError as exc:
            raise OpError(str(exc)) from exc
        rows = list(
            ctx.session.scalars(
                select(ActivityRow)
                .where(ActivityRow.series_id == series.id)
                .order_by(ActivityRow.earliest_start_at)
            )
        )
        for row in rows:
            row.locked = body.locked
        return {
            "series_id": str(series.id),
            "occurrences_created": len(rows),
            "first": activity_out(rows[0]) if rows else None,
        }
    row = ActivityRow(
        kind=body.kind,
        title=body.title,
        notes=body.notes,
        scheduling=scheduling,
        duration_minutes=duration,
        earliest_start_at=begin,
        latest_end_at=finish,
        preferred_start_at=preferred,
        place_id=body.place_id,
        all_day=body.all_day,
        busy=body.busy,
        remote=body.remote,
        priority=body.priority,
        locked=body.locked and scheduling == "fixed",
        source="assistant",
    )
    ctx.session.add(row)
    ctx.session.flush()
    return {"activity": activity_out(row)}


class EventUpdateIn(Strict):
    activity_id: str
    scope: Literal["occurrence", "following", "series"] = Field(
        default="occurrence", description="For recurring events: this one, this and following, all"
    )
    title: str | None = Field(default=None, max_length=300)
    start: datetime | None = Field(default=None, description="New fixed start (keeps duration)")
    duration_minutes: int | None = Field(default=None, gt=0, le=24 * 60)
    window_start: datetime | None = None
    window_end: datetime | None = None
    scheduling: Literal["fixed", "flexible"] | None = None
    place_id: str | None = None
    remote: bool | None = None
    busy: bool | None = None
    priority: int | None = Field(default=None, ge=1, le=3)
    notes: str | None = Field(default=None, max_length=5000)


def _activity(ctx: OpContext, activity_id: str) -> ActivityRow:
    row = ctx.session.get(ActivityRow, _uuid(activity_id, "activity"), with_for_update=True)
    if row is None:
        raise OpError("activité introuvable")
    return row


def _apply_event_changes(ctx: OpContext, row: ActivityRow, body: EventUpdateIn) -> list[str]:
    changed: list[str] = []
    if row.kind == "race" and (body.start or body.scheduling == "flexible"):
        raise OpError("une compétition est fixe : son horaire vient de sa source")
    if body.title is not None:
        row.title = body.title
        changed.append("title")
    if body.notes is not None:
        row.notes = body.notes
        changed.append("notes")
    if body.place_id is not None:
        _place_exists(ctx, body.place_id or None)
        row.place_id = body.place_id or None
        changed.append("place_id")
    for name in ("remote", "busy", "priority"):
        value = getattr(body, name)
        if value is not None:
            setattr(row, name, value)
            changed.append(name)
    if body.scheduling is not None and body.scheduling != row.scheduling:
        row.scheduling = body.scheduling
        changed.append("scheduling")
    if body.duration_minutes is not None:
        row.duration_minutes = body.duration_minutes
        changed.append("duration_minutes")
    duration = timedelta(minutes=row.duration_minutes or 0)
    if body.start is not None:
        begin = _local(body.start, ctx.tz)
        if row.scheduling == "fixed":
            row.earliest_start_at, row.latest_end_at = begin, begin + duration
        else:
            row.planned_start_at = begin
            row.locked = True  # an explicit time for a flexible activity pins it
        changed.append("start")
    elif "duration_minutes" in changed and row.scheduling == "fixed":
        row.latest_end_at = row.earliest_start_at + duration
    if body.window_start is not None or body.window_end is not None:
        begin = _local(body.window_start or row.earliest_start_at, ctx.tz)
        finish = _local(body.window_end or row.latest_end_at, ctx.tz)
        if finish - begin < duration:
            raise OpError("fenêtre plus courte que la durée")
        row.earliest_start_at, row.latest_end_at = begin, finish
        changed.append("window")
    if row.latest_end_at <= row.earliest_start_at:
        raise OpError("horaire incohérent")
    row.user_modified = True
    return changed


def _overrides(body: EventUpdateIn, tz: ZoneInfo) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in (
        "title",
        "duration_minutes",
        "place_id",
        "remote",
        "busy",
        "priority",
        "notes",
        "scheduling",
    ):
        value = getattr(body, name)
        if value is not None:
            values[name] = value
    if body.start is not None:
        values["start"] = _local(body.start, tz).isoformat()
    return values


@operation(
    "update_event",
    "Modify or move an activity (personal event, training, task session). For a recurring "
    "event choose scope occurrence / following / series. Moving a training changes only its "
    "day/time, never its prescribed content. Races are fixed.",
    EventUpdateIn,
    mutates=True,
    replan=True,
)
def update_event(ctx: OpContext, body: EventUpdateIn) -> dict[str, Any]:
    row = _activity(ctx, body.activity_id)
    if row.status not in ("active", "to_confirm"):
        raise OpError(f"activité {row.status} : non modifiable")
    if row.series_id is None or body.scope == "occurrence":
        changed = _apply_event_changes(ctx, row, body)
        if row.series_id is not None:
            edit_occurrence(ctx.session, row, overrides=_overrides(body, ctx.tz))
        return {"activity": activity_out(row), "changed": changed}
    series = ctx.session.get(RecurrenceSeriesRow, row.series_id)
    if series is None:
        raise OpError("série introuvable")
    try:
        if body.scope == "series" and body.start is None:
            changes = {k: v for k, v in _overrides(body, ctx.tz).items() if k != "start"}
            updated = update_series(ctx.session, series, changes, ctx.now)
            return {"series_id": str(series.id), "occurrences_updated": updated}
        # Time change of the series, or "this and following": split the series.
        key = row.occurrence_key or ""
        template = {
            **series.template,
            **{k: v for k, v in _overrides(body, ctx.tz).items() if k != "start"},
        }
        first = _local(body.start, ctx.tz) if body.start is not None else row.earliest_start_at
        if body.scope == "series":
            key = min(
                (
                    r.occurrence_key or ""
                    for r in ctx.session.scalars(
                        select(ActivityRow).where(
                            ActivityRow.series_id == series.id,
                            ActivityRow.status == "active",
                            ActivityRow.earliest_start_at >= ctx.now,
                        )
                    )
                ),
                default=key,
            )
        end_series_before(ctx.session, series, key, ctx.now)
        rule = ";".join(
            p for p in series.rrule.split(";") if not p.upper().startswith(("UNTIL=", "COUNT="))
        )
        new = create_series(
            ctx.session, rule=rule, first_start=first, tz=ctx.tz, template=template, now=ctx.now
        )
    except DomainError as exc:
        raise OpError(str(exc)) from exc
    return {"previous_series_id": str(series.id), "series_id": str(new.id)}


class CancelEventIn(Strict):
    activity_id: str
    scope: Literal["occurrence", "following", "series"] = "occurrence"


@operation(
    "cancel_event",
    "Cancel an activity (kept in history), or a recurring event's occurrence / following / "
    "whole series. University classes cannot be cancelled here (iCal is the authority).",
    CancelEventIn,
    mutates=True,
    replan=True,
)
def cancel_event(ctx: OpContext, body: CancelEventIn) -> dict[str, Any]:
    row = _activity(ctx, body.activity_id)
    if row.series_id is None or body.scope == "occurrence":
        if row.series_id is not None:
            edit_occurrence(ctx.session, row, cancel=True)
        row.status = "cancelled"
        return {"cancelled": 1}
    series = ctx.session.get(RecurrenceSeriesRow, row.series_id)
    if series is None:
        raise OpError("série introuvable")
    if body.scope == "series":
        return {"cancelled": cancel_series(ctx.session, series, ctx.now)}
    before = sum(
        1
        for _ in ctx.session.scalars(
            select(ActivityRow.id).where(
                ActivityRow.series_id == series.id, ActivityRow.status == "cancelled"
            )
        )
    )
    end_series_before(ctx.session, series, row.occurrence_key or "", ctx.now)
    after = sum(
        1
        for _ in ctx.session.scalars(
            select(ActivityRow.id).where(
                ActivityRow.series_id == series.id, ActivityRow.status == "cancelled"
            )
        )
    )
    return {"cancelled": after - before}


class LockIn(Strict):
    target_id: str = Field(
        description="activity id or study block id (block:<id> or activity:<id>)"
    )
    locked: bool


@operation(
    "set_lock",
    "Lock (protect from automatic moves) or unlock an activity or a study block.",
    LockIn,
    mutates=True,
    replan=True,
)
def set_lock(ctx: OpContext, body: LockIn) -> dict[str, Any]:
    if (
        body.target_id.startswith("block:")
        or ctx.session.get(StudyBlockRow, _uuid(body.target_id, "block")) is not None
    ):
        block_id = _uuid(body.target_id.removeprefix("block:"), "block")
        block = ctx.session.get(StudyBlockRow, block_id)
        if block is None:
            raise OpError("séance introuvable")
        if body.locked and not block.locked and block.state not in ("promoted", "done"):
            # The user protects this block: a locked copy is never moved without approval.
            ctx.session.add(
                StudyBlockRow(
                    task_id=block.task_id, start_at=block.start_at, end_at=block.end_at, locked=True
                )
            )
            block.state = "promoted"
        elif not body.locked and block.locked:
            ctx.session.delete(block)
        return {"block_id": str(block_id), "locked": body.locked}
    row = _activity(ctx, body.target_id)
    if body.locked and row.scheduling == "flexible" and row.planned_start_at is None:
        raise OpError("activité pas encore placée : rien à verrouiller")
    row.locked = body.locked
    row.user_modified = True
    return {"activity": activity_out(row)}


class ReportIn(Strict):
    activity_id: str
    outcome: Literal["done", "partial", "missed"]
    actual_minutes: int | None = Field(default=None, ge=1, le=24 * 60)


@operation(
    "report_activity",
    "Record that an activity (training, event, task session) was done, partly done or missed. "
    "Never assumed from the clock: only what the user says.",
    ReportIn,
    mutates=True,
    replan=True,
)
def report_activity(ctx: OpContext, body: ReportIn) -> dict[str, Any]:
    row = _activity(ctx, body.activity_id)
    row.status = body.outcome
    if row.task_id is not None and body.outcome != "missed":
        task = ctx.session.get(StudyTaskRow, row.task_id)
        minutes = body.actual_minutes or (row.duration_minutes if body.outcome == "done" else None)
        if task is not None and minutes:
            record_work(ctx.session, task, minutes, now=ctx.now, key=f"activity:{row.id}")
    return {"activity": activity_out(row)}


class ConfirmIn(Strict):
    activity_id: str
    duration_minutes: int = Field(gt=0, le=24 * 60)
    start: datetime | None = Field(default=None, description="Race start (required for a race)")


@operation(
    "confirm_activity",
    "Give the duration (and race start) a coach plan did not state.",
    ConfirmIn,
    mutates=True,
    replan=True,
)
def confirm_activity(ctx: OpContext, body: ConfirmIn) -> dict[str, Any]:
    row = ctx.session.get(ActivityRow, _uuid(body.activity_id, "activity"), with_for_update=True)
    if row is None:
        raise OpError("activité introuvable")
    if row.status != "to_confirm":
        raise OpError(f"activité déjà {row.status}")
    duration = timedelta(minutes=body.duration_minutes)
    if row.kind == "race":
        if body.start is None:
            raise OpError("une course a besoin de son heure de départ")
        start = _local(body.start, ctx.tz)
        if not row.earliest_start_at <= start < row.latest_end_at:
            raise OpError("le départ doit être le jour de la course")
        row.earliest_start_at, row.latest_end_at = start, start + duration
    elif row.latest_end_at - row.earliest_start_at < duration:
        raise OpError("plus long que sa fenêtre")
    row.duration_minutes = body.duration_minutes
    row.status = "active"
    row.user_modified = True
    return {"activity": activity_out(row)}


# --- tasks ---


class TaskIn(Strict):
    title: str = Field(min_length=1, max_length=200, description="Study subject or task title")
    kind: Literal["study", "personal"] = "study"
    estimated_minutes: int | None = Field(
        default=None, gt=0, le=100_000, description="Unknown: omit"
    )
    target_date: date | None = None
    hard_deadline: bool = Field(default=False, description="True only if explicitly strict")
    priority: int = Field(default=2, ge=1, le=3)
    splittable: bool = True
    place_id: str | None = None
    notes: str = Field(default="", max_length=5000)


@operation(
    "create_task",
    "Create a task or a study volume (e.g. 15 h of physics before Friday). Target dates are "
    "soft unless the user explicitly says strict. Without an estimated duration the task is "
    "kept but not planned. A located or non-splittable task is planned as one session.",
    TaskIn,
    mutates=True,
    replan=True,
)
def create_task(ctx: OpContext, body: TaskIn) -> dict[str, Any]:
    _place_exists(ctx, body.place_id)
    if body.target_date is not None and body.target_date < ctx.now.astimezone(ctx.tz).date():
        raise OpError("échéance déjà passée")
    row = StudyTaskRow(
        subject=body.title,
        kind=body.kind,
        effort_minutes=body.estimated_minutes,
        target_date=body.target_date,
        target_kind=TargetKind.HARD if body.hard_deadline else TargetKind.SOFT,
        priority=body.priority,
        splittable=body.splittable,
        place_id=body.place_id,
        notes=body.notes,
    )
    ctx.session.add(row)
    ctx.session.flush()
    sync_task_session(ctx.session, row, ctx.now)
    return {"task": task_out(row)}


class TaskUpdateIn(Strict):
    task_id: str
    title: str | None = Field(default=None, max_length=200)
    estimated_minutes: int | None = Field(default=None, gt=0, le=100_000)
    target_date: date | None = None
    clear_target_date: bool = False
    hard_deadline: bool | None = None
    priority: int | None = Field(default=None, ge=1, le=3)
    splittable: bool | None = None
    place_id: str | None = None
    notes: str | None = Field(default=None, max_length=5000)
    archived: bool | None = Field(default=None, description="True = abandon / delete the task")


def _task(ctx: OpContext, task_id: str) -> StudyTaskRow:
    row = ctx.session.get(StudyTaskRow, _uuid(task_id, "task"), with_for_update=True)
    if row is None:
        raise OpError("tâche introuvable")
    return row


@operation(
    "update_task",
    "Change a task: estimate, target date (soft/strict), priority, splitting, place, notes, or "
    "archive it. Use log_work for progress.",
    TaskUpdateIn,
    mutates=True,
    replan=True,
)
def update_task(ctx: OpContext, body: TaskUpdateIn) -> dict[str, Any]:
    row = _task(ctx, body.task_id)
    if body.title is not None:
        row.subject = body.title
    if body.estimated_minutes is not None:
        row.effort_minutes = body.estimated_minutes
    if body.clear_target_date:
        row.target_date = None
    elif body.target_date is not None:
        row.target_date = body.target_date
    if body.hard_deadline is not None:
        row.target_kind = TargetKind.HARD if body.hard_deadline else TargetKind.SOFT
    for name in ("priority", "splittable", "notes", "archived"):
        value = getattr(body, name)
        if value is not None:
            setattr(row, name, value)
    if body.place_id is not None:
        _place_exists(ctx, body.place_id or None)
        row.place_id = body.place_id or None
    sync_task_session(ctx.session, row, ctx.now)
    return {"task": task_out(row)}


class WorkIn(Strict):
    task_id: str | None = None
    block_id: str | None = Field(default=None, description="Study block done (all or part)")
    minutes: int | None = Field(
        default=None, ge=-100_000, le=24 * 60, description="Negative = correction"
    )
    note: str = Field(default="", max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=150)


@operation(
    "log_work",
    "Record work actually done: a study block (fully, or minutes of it) or minutes on a task. "
    "A negative value corrects an earlier report. Remaining work is recomputed from the log.",
    WorkIn,
    mutates=True,
    replan=True,
)
def log_work(ctx: OpContext, body: WorkIn) -> dict[str, Any]:
    try:
        if body.block_id is not None:
            block = ctx.session.get(
                StudyBlockRow, _uuid(body.block_id, "block"), with_for_update=True
            )
            if block is None:
                raise OpError("séance introuvable")
            if block.state in ("done", "partial"):
                raise OpError(f"séance déjà déclarée ({block.state})")
            worked = complete_block(ctx.session, block, ctx.now, body.minutes)
            block.locked = False
            task = ctx.session.get(StudyTaskRow, block.task_id)
            return {"logged_minutes": worked, "task": None if task is None else task_out(task)}
        if body.task_id is None or not body.minutes:
            raise OpError("indiquez une tâche et une durée, ou une séance")
        task = _task(ctx, body.task_id)
        key = None if body.idempotency_key is None else f"user:{body.idempotency_key}"
        inserted = record_work(
            ctx.session, task, body.minutes, now=ctx.now, key=key, note=body.note
        )
    except DomainError as exc:
        raise OpError(str(exc)) from exc
    return {
        "logged_minutes": body.minutes if inserted else 0,
        "duplicate": not inserted,
        "task": task_out(task),
    }


# --- places, rules, location ---


class PlaceIn(Strict):
    place_id: str = Field(pattern=r"^[a-z0-9_]{1,50}$")
    name: str = Field(min_length=1, max_length=200)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    aliases: list[str] = Field(default_factory=list, max_length=50)


@operation(
    "upsert_place",
    "Create or update a known place with coordinates the user gave (never guessed) and "
    "aliases (e.g. iCal location texts).",
    PlaceIn,
    mutates=True,
    replan=True,
)
def upsert_place(ctx: OpContext, body: PlaceIn) -> dict[str, Any]:
    row = ctx.session.get(PlaceRow, body.place_id) or PlaceRow(id=body.place_id)
    row.name, row.latitude, row.longitude = body.name, body.latitude, body.longitude
    ctx.session.add(row)
    ctx.session.flush()
    ctx.session.execute(delete(PlaceAliasRow).where(PlaceAliasRow.place_id == body.place_id))
    for alias in {" ".join(a.split()).casefold() for a in body.aliases if a.strip()}:
        existing = ctx.session.get(PlaceAliasRow, alias)
        if existing is not None and existing.place_id != body.place_id:
            raise OpError(f"alias « {alias} » déjà utilisé par {existing.place_id}")
        ctx.session.add(PlaceAliasRow(alias=alias, place_id=body.place_id))
    return {"place_id": body.place_id}


class PlaceIdIn(Strict):
    place_id: str


@operation("delete_place", "Delete a known place.", PlaceIdIn, mutates=True, replan=True)
def delete_place(ctx: OpContext, body: PlaceIdIn) -> dict[str, Any]:
    if get_setting(ctx.session, "home_place_id") == body.place_id:
        raise OpError("ce lieu est le domicile configuré")
    row = ctx.session.get(PlaceRow, body.place_id)
    if row is None:
        raise OpError("lieu introuvable")
    ctx.session.delete(row)
    return {"deleted": body.place_id}


class SettingsChangeIn(Strict):
    home_place_id: str | None = None
    tracker_entity_id: str | None = Field(
        default=None, pattern=r"^device_tracker\.[a-z0-9_]{1,200}$"
    )
    horizon_days: int | None = Field(default=None, ge=1, le=42)
    buffer_minutes: int | None = Field(default=None, ge=0, le=120)
    departure_reminder_minutes: list[Annotated[int, Field(ge=0, le=240)]] | None = Field(
        default=None, max_length=5, description="Minutes before the advised leave time"
    )
    study_preferences: dict[str, int] | None = Field(
        default=None,
        description="preferred_block_minutes, min_block_minutes, max_block_minutes, "
        "min_gap_same_subject_minutes, max_daily_study_minutes, max_blocks_per_subject_per_day",
    )


@operation(
    "update_settings",
    "Permanent preferences: home place, phone tracker, planning horizon, safety margin before "
    "appointments, departure reminders (minutes before leaving, several allowed), study "
    "preferences (90-minute blocks...).",
    SettingsChangeIn,
    mutates=True,
    replan=True,
)
def update_settings(ctx: OpContext, body: SettingsChangeIn) -> dict[str, Any]:
    values = body.model_dump(exclude_none=True)
    if "departure_reminder_minutes" in values:
        values["reminder_minutes"] = values.pop("departure_reminder_minutes")
    if "home_place_id" in values:
        _place_exists(ctx, values["home_place_id"])
    if "study_preferences" in values:
        current = get_setting(ctx.session, "study_preferences", {}) or {}
        values["study_preferences"] = {**current, **values["study_preferences"]}
        try:
            Preferences(**values["study_preferences"])
        except (TypeError, ValueError) as exc:
            raise OpError(f"préférences d'étude invalides : {exc}") from exc
    for key, value in values.items():
        set_setting(ctx.session, key, value)
    return {"updated": sorted(values)}


class WindowIn(Strict):
    weekday: int = Field(ge=0, le=6, description="0 = Monday")
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^(\d{2}:\d{2}|24:00)$")
    place_id: str


class WeeklyIn(Strict):
    windows: list[WindowIn] = Field(max_length=200)


@operation(
    "set_weekly_availability",
    "Replace the permanent weekly windows in which flexible work may be planned (with the "
    "place where the user is then). Read get_rules first and resend the full list.",
    WeeklyIn,
    mutates=True,
    replan=True,
)
def set_weekly_availability(ctx: OpContext, body: WeeklyIn) -> dict[str, Any]:
    rows = []
    for window in body.windows:
        start, end = _minute(window.start), _minute(window.end)
        if end <= start:
            raise OpError("fin avant début")
        _place_exists(ctx, window.place_id)
        rows.append(
            AvailabilityRow(
                weekday=window.weekday, start_minute=start, end_minute=end, place_id=window.place_id
            )
        )
    ctx.session.execute(delete(AvailabilityRow))
    ctx.session.add_all(rows)
    return {"windows": len(rows)}


class CategoryWindowIn(Strict):
    weekday: int = Field(ge=0, le=6, description="0 = Monday")
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^(\d{2}:\d{2}|24:00)$")


class CategoryWindowsIn(Strict):
    kind: Literal["training", "personal", "task"] = Field(
        description="Category of flexible activities (task = located task session)"
    )
    windows: list[CategoryWindowIn] = Field(
        max_length=100, description="Full list; empty = remove the restriction"
    )


def _category_windows(session: Session) -> dict[str, list[dict[str, Any]]]:
    rows = session.scalars(
        select(CategoryWindowRow).order_by(
            CategoryWindowRow.kind, CategoryWindowRow.weekday, CategoryWindowRow.start_minute
        )
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row.kind, []).append(
            {
                "weekday": row.weekday,
                "start": _minutes_text(row.start_minute),
                "end": _minutes_text(row.end_minute),
            }
        )
    return out


@operation(
    "set_category_windows",
    "Replace the weekly windows in which flexible activities of one category may be placed "
    "(e.g. trainings only 07:00-09:00 on weekdays). Hard rule: a session that does not fit "
    "stays unplaced and is reported. Empty list = no restriction. Read get_rules first.",
    CategoryWindowsIn,
    mutates=True,
    replan=True,
)
def set_category_windows(ctx: OpContext, body: CategoryWindowsIn) -> dict[str, Any]:
    rows = []
    for window in body.windows:
        start, end = _minute(window.start), _minute(window.end)
        if end <= start:
            raise OpError("fin avant début")
        rows.append(
            CategoryWindowRow(
                kind=body.kind, weekday=window.weekday, start_minute=start, end_minute=end
            )
        )
    for old in ctx.session.scalars(
        select(CategoryWindowRow).where(CategoryWindowRow.kind == body.kind)
    ):
        ctx.session.delete(old)
    ctx.session.flush()
    ctx.session.add_all(rows)
    return {"kind": body.kind, "windows": len(rows)}


def _minute(text: str) -> int:
    hours, minutes = text.split(":")
    value = int(hours) * 60 + int(minutes)
    if not 0 <= value <= 1440:
        raise OpError(f"heure invalide : {text}")
    return value


class ExceptionIn(Strict):
    day: date
    start: str = Field(default="00:00", pattern=r"^\d{2}:\d{2}$")
    end: str = Field(default="24:00", pattern=r"^(\d{2}:\d{2}|24:00)$")
    available: bool = Field(description="True = extra free time; False = blocked time")
    place_id: str | None = Field(default=None, description="Required when available")
    reason: str = Field(default="", max_length=300)


@operation(
    "add_availability_exception",
    "Dated exception to the weekly rules: block time (not available) or open extra time "
    "(available, at a place). Does not override fixed commitments.",
    ExceptionIn,
    mutates=True,
    replan=True,
)
def add_availability_exception(ctx: OpContext, body: ExceptionIn) -> dict[str, Any]:
    start, end = _minute(body.start), _minute(body.end)
    if end <= start:
        raise OpError("fin avant début")
    if body.available and body.place_id is None:
        raise OpError("indiquez où l'utilisateur sera pendant ce temps libre")
    _place_exists(ctx, body.place_id)
    row = AvailabilityExceptionRow(
        day=body.day,
        start_minute=start,
        end_minute=end,
        available=body.available,
        place_id=body.place_id,
        reason=body.reason,
    )
    ctx.session.add(row)
    ctx.session.flush()
    return {"exception_id": str(row.id)}


class IdIn(Strict):
    id: str


@operation(
    "delete_availability_exception",
    "Remove a dated availability exception.",
    IdIn,
    mutates=True,
    replan=True,
)
def delete_availability_exception(ctx: OpContext, body: IdIn) -> dict[str, Any]:
    row = ctx.session.get(AvailabilityExceptionRow, _uuid(body.id, "exception"))
    if row is None:
        raise OpError("exception introuvable")
    ctx.session.delete(row)
    return {"deleted": body.id}


class LocationIntentIn(Strict):
    place_id: str
    start: datetime
    end: datetime


@operation(
    "set_planned_location",
    "Where the user WILL be (base for the days covered, e.g. 'tomorrow I sleep at dad's'). "
    "Used to infer future travel origins; never changes the real GPS position.",
    LocationIntentIn,
    mutates=True,
    replan=True,
)
def set_planned_location(ctx: OpContext, body: LocationIntentIn) -> dict[str, Any]:
    _place_exists(ctx, body.place_id)
    start, end = _local(body.start, ctx.tz), _local(body.end, ctx.tz)
    if end <= start or end - start > timedelta(days=60):
        raise OpError("période invalide (au plus 60 jours)")
    stays = [
        s
        for s in get_setting(ctx.session, "stays", []) or []
        if datetime.fromisoformat(s["end"]) > ctx.now
        and not (
            datetime.fromisoformat(s["start"]) < end and start < datetime.fromisoformat(s["end"])
        )
    ]
    stays.append({"place_id": body.place_id, "start": start.isoformat(), "end": end.isoformat()})
    set_setting(ctx.session, "stays", stays[-50:])
    return {"planned_locations": stays}


class ClearLocationIn(Strict):
    start: datetime
    end: datetime


@operation(
    "clear_planned_location",
    "Remove planned locations overlapping a period.",
    ClearLocationIn,
    mutates=True,
    replan=True,
)
def clear_planned_location(ctx: OpContext, body: ClearLocationIn) -> dict[str, Any]:
    start, end = _local(body.start, ctx.tz), _local(body.end, ctx.tz)
    stays = [
        s
        for s in get_setting(ctx.session, "stays", []) or []
        if not (
            datetime.fromisoformat(s["start"]) < end and start < datetime.fromisoformat(s["end"])
        )
    ]
    set_setting(ctx.session, "stays", stays)
    return {"planned_locations": stays}


# --- reminders, proposals, notifications ---


class ReminderIn(Strict):
    message: str = Field(min_length=1, max_length=300)
    at: datetime | None = Field(default=None, description="Absolute time")
    target_id: str | None = Field(default=None, description="activity:<id> or event:<id> (class)")
    offset_minutes: int = Field(
        default=0, ge=0, le=7 * 24 * 60, description="Before the target start"
    )
    rrule: str | None = Field(default=None, description="Recurring absolute reminder (RRULE)")


@operation(
    "create_reminder",
    "Reminder at a time (optionally recurring) or N minutes before an activity / class; "
    "relative reminders follow the target when it moves. Departure reminders are settings.",
    ReminderIn,
    mutates=True,
)
def create_reminder(ctx: OpContext, body: ReminderIn) -> dict[str, Any]:
    from tempo.domain.ops import validate_rrule

    if (body.at is None) == (body.target_id is None):
        raise OpError("indiquez soit une heure, soit une activité cible")
    row = ReminderRow(message=body.message, offset_minutes=body.offset_minutes)
    if body.at is not None:
        row.target_type, row.remind_at = "time", _local(body.at, ctx.tz)
        if body.rrule:
            try:
                row.rrule = validate_rrule(body.rrule)
            except DomainError as exc:
                raise OpError(str(exc)) from exc
        elif row.remind_at <= ctx.now:
            raise OpError("heure déjà passée")
    else:
        assert body.target_id is not None  # noqa: S101 - checked above
        kind, _, raw = body.target_id.partition(":")
        if kind == "event":
            if ctx.session.get(FixedEventRow, _uuid(raw, "event")) is None:
                raise OpError("cours introuvable")
        elif kind == "activity":
            _activity(ctx, raw)
        else:
            raise OpError("cible : activity:<id> ou event:<id>")
        row.target_type, row.target_id = kind, raw
    ctx.session.add(row)
    ctx.session.flush()
    return {"reminder": reminder_out(row)}


@operation("cancel_reminder", "Cancel a reminder.", IdIn, mutates=True)
def cancel_reminder(ctx: OpContext, body: IdIn) -> dict[str, Any]:
    row = ctx.session.get(ReminderRow, _uuid(body.id, "reminder"))
    if row is None:
        raise OpError("rappel introuvable")
    row.status = "cancelled"
    return {"reminder": reminder_out(row)}


class DecideIn(Strict):
    proposal_id: str
    approve: bool


@operation(
    "decide_proposal",
    "Apply the user's explicit natural-language decision on ONE identified proposal. Never "
    "decide without an explicit answer. A proposal whose inputs changed becomes obsolete.",
    DecideIn,
    mutates=True,
    replan=True,
)
def decide_proposal(ctx: OpContext, body: DecideIn) -> dict[str, Any]:
    return {
        "proposal": decide(
            ctx.session,
            _uuid(body.proposal_id, "proposal"),
            body.approve,
            actor=ctx.actor,
            now=ctx.now,
        )
    }


@operation("acknowledge_notification", "Mark a notification as read.", IdIn, mutates=True)
def acknowledge_notification(ctx: OpContext, body: IdIn) -> dict[str, Any]:
    row = ctx.session.get(NotificationRow, _uuid(body.id, "notification"))
    if row is None:
        raise OpError("notification introuvable")
    row.acknowledged_at = row.acknowledged_at or ctx.now
    return {"acknowledged": body.id}


# --- planning and transport ---


class PlanIn(Strict):
    mode: Literal["local", "global"] = Field(
        default="local",
        description="global ONLY when the user explicitly asks to reorganise everything",
    )
    dry_run: bool = Field(
        default=False, description="Simulate: compute and report, publish nothing"
    )


@operation(
    "plan_now",
    "Re-plan now and return the REAL outcome: status, completeness, unplanned minutes, "
    "conflicts and what moved. Local mode keeps the existing plan as much as possible. "
    "dry_run simulates an alternative without publishing it.",
    PlanIn,
    mutates=True,
)
def plan_now(ctx: OpContext, body: PlanIn) -> dict[str, Any]:
    if ctx.routing is None:
        set_setting(ctx.session, "replan_requested", ctx.now.isoformat())
        return {
            "status": "requested",
            "detail": "routage indisponible dans l'API : le worker replanifie sous une minute",
        }
    savepoint = ctx.session.begin_nested()
    try:
        version = replan(
            ctx.session, ctx.routing, ctx.now, ctx.tz, reason="assistant", mode=body.mode
        )
        plan = dict(version.summary)
        result = {
            "published": version.state == "active" and not body.dry_run,
            "state": version.state,
            "solver_status": version.solver_status,
            "status": plan.get("status"),
            "diff": plan.get("diff"),
            "unplanned_minutes": plan.get("unplanned_minutes"),
            "hard_deadline_misses": plan.get("hard_deadline_misses"),
            "unplaced_activities": plan.get("unplaced_trainings"),
            "issues": plan.get("issues"),
            "violations": plan.get("violations"),
        }
    except PlanningNotConfiguredError as exc:
        savepoint.rollback()
        raise OpError(f"planification impossible : {exc}") from exc
    except RoutingUnavailableError as exc:
        savepoint.rollback()
        raise OpError(
            f"routage indisponible : {exc} (ce n'est pas une preuve d'impossibilité)"
        ) from exc
    if body.dry_run:
        savepoint.rollback()
    else:
        savepoint.commit()
        set_setting(ctx.session, "replan_requested", None)
    explain = explain_plan(ctx, NoInput())["active_plan"] if not body.dry_run else None
    if explain is not None:
        result["explanation"] = {
            k: explain.get(k) for k in ("unplanned_minutes", "late_minutes", "unplaced_activities")
        }
    return result


POSTPONE_STEPS_DAYS = (2, 7, 14, 28)
MAX_ALTERNATIVE_TASKS = 3


def _simulate(ctx: OpContext, change: Callable[[StudyTaskRow], None], task_id: str) -> Any:
    """Apply ``change`` to the task, re-plan, and roll everything back (nothing published)."""
    assert ctx.routing is not None  # noqa: S101 - checked by the caller
    savepoint = ctx.session.begin_nested()
    try:
        row = ctx.session.get(StudyTaskRow, uuid.UUID(task_id))
        assert row is not None  # noqa: S101 - listed from the active plan
        change(row)
        ctx.session.flush()
        version = replan(ctx.session, ctx.routing, ctx.now, ctx.tz, reason="simulation")
        plan = dict(version.summary)
    finally:
        savepoint.rollback()
    unplanned = int(plan.get("unplanned_minutes", {}).get(task_id, 0))
    return {
        "valid": version.state == "active",
        "unplanned_minutes": unplanned,
        "late_minutes": int(plan.get("late_minutes", {}).get(task_id, 0)),
        "strict_miss": task_id in plan.get("hard_deadline_misses", []),
        "resolves": version.state == "active"
        and unplanned == 0
        and task_id not in plan.get("hard_deadline_misses", []),
    }


@operation(
    "propose_alternatives",
    "When the active plan is partial (remainder or missed strict deadline), SIMULATE "
    "deterministic alternatives per affected task: postpone the deadline (smallest tried "
    "postponement that resolves), make a strict deadline soft, or reduce the volume to what "
    "fits. Nothing is published or changed; apply one only after the user agrees.",
    NoInput,
    mutates=False,
)
def propose_alternatives(ctx: OpContext, _: NoInput) -> dict[str, Any]:
    version = active_version(ctx.session)
    if version is None:
        return {"status": "no_plan", "detail": "aucun planning publié : lancez plan_now"}
    plan = dict(version.summary)
    unplanned: dict[str, int] = plan.get("unplanned_minutes", {}) or {}
    misses: list[str] = plan.get("hard_deadline_misses", []) or []
    affected = [t for t in dict.fromkeys([*misses, *unplanned]) if misses.count(t) or unplanned[t]]
    if not affected:
        return {"status": "nothing_to_resolve", "detail": "tout le travail est planifié"}
    if ctx.routing is None:
        return {"status": "unavailable", "detail": "routage indisponible : simulation impossible"}
    tasks: list[dict[str, Any]] = []
    for task_id in affected[:MAX_ALTERNATIVE_TASKS]:
        row = ctx.session.get(StudyTaskRow, uuid.UUID(task_id))
        if row is None:
            continue
        alternatives: list[dict[str, Any]] = []
        missing = int(unplanned.get(task_id, 0))
        remaining = remaining_minutes(row) or 0
        if missing and remaining > missing:
            alternatives.append(
                {
                    "kind": "reduce_volume",
                    "plannable_minutes": remaining - missing,
                    "resolves": True,
                    "detail": "ne garder que le volume qui tient dans l'horizon actuel",
                }
            )
        try:
            if row.target_date is not None:
                original = row.target_date
                for days in POSTPONE_STEPS_DAYS:
                    new_date = original + timedelta(days=days)

                    def postpone(task: StudyTaskRow, when: date = new_date) -> None:
                        task.target_date = when

                    outcome = _simulate(ctx, postpone, task_id)
                    if outcome["resolves"] or days == POSTPONE_STEPS_DAYS[-1]:
                        alternatives.append(
                            {
                                "kind": "postpone_deadline",
                                "new_target_date": new_date.isoformat(),
                                **outcome,
                            }
                        )
                        break
            if row.target_kind == TargetKind.HARD:

                def soften(task: StudyTaskRow) -> None:
                    task.target_kind = TargetKind.SOFT

                alternatives.append(
                    {"kind": "make_deadline_soft", **_simulate(ctx, soften, task_id)}
                )
        except RoutingUnavailableError as exc:
            return {"status": "error", "detail": f"routage indisponible : {exc}"}
        except PlanningNotConfiguredError as exc:
            raise OpError(f"planification impossible : {exc}") from exc
        tasks.append(
            {
                "task_id": task_id,
                "title": row.subject,
                "unplanned_minutes": missing,
                "strict_miss": task_id in misses,
                "alternatives": alternatives,
            }
        )
    return {
        "status": "alternatives",
        "tasks": tasks,
        "note": "simulations seulement : rien n'est appliqué sans accord explicite "
        "(update_task puis plan_now)",
    }


class JourneyIn(Strict):
    origin_place_id: str | None = Field(default=None, description="Known place; or use_gps")
    use_gps: bool = Field(
        default=False, description="Origin = current phone position (immediate trip)"
    )
    destination_place_id: str
    depart_after: datetime | None = None
    arrive_before: datetime | None = None
    objective: Literal["shortest", "earliest_arrival", "latest_departure"] = "shortest"


def _journey_out(journey: Journey) -> dict[str, Any]:
    return {
        "departure": _iso(journey.departure),
        "arrival": _iso(journey.arrival),
        "duration_minutes": int(journey.duration.total_seconds() // 60),
        "transfers": journey.transfers,
        "realtime": any(leg.realtime for leg in journey.legs),
        "legs": [
            {
                "mode": leg.mode,
                "line": leg.line,
                "operator": leg.agency,
                "from": leg.origin_name,
                "to": leg.destination_name,
                "start": _iso(leg.start),
                "end": _iso(leg.end),
                "realtime": leg.realtime,
                "cancelled": leg.cancelled,
            }
            for leg in journey.legs
        ],
    }


MAX_JOURNEY_PAGES = 5
OPEN_SEARCH_SPAN = timedelta(hours=2)
"""Exploration span after the only lower bound (or before the only upper bound)."""


def _explore_journeys(
    routing: RoutingProvider,
    origin: Coordinates,
    target: Coordinates,
    lower: datetime | None,
    upper: datetime | None,
) -> tuple[list[Journey], int, bool]:
    """Page through the provider's answers (a MOTIS answer holds only a few itineraries).

    With a lower bound, pages move forward from it (up to the upper bound, or
    ``OPEN_SEARCH_SPAN``); with only an upper bound, arrive-by pages move backward. At most
    ``MAX_JOURNEY_PAGES`` requests. A failure on the first page propagates; a later failure
    keeps what was explored and is reported as an incomplete exploration."""
    found: dict[tuple[Any, ...], Journey] = {}
    pages = 0
    forward = lower is not None
    if forward:
        assert lower is not None  # noqa: S101 - narrowing
        cursor = lower
        stop = upper if upper is not None else lower + OPEN_SEARCH_SPAN
    else:
        assert upper is not None  # noqa: S101 - one bound is required by the caller
        cursor = upper
        stop = upper - OPEN_SEARCH_SPAN
    while pages < MAX_JOURNEY_PAGES and (cursor < stop if forward else cursor > stop):
        try:
            page = routing.plan(origin, target, cursor, arrive_by=not forward)
        except RoutingUnavailableError:
            if pages == 0:
                raise
            return list(found.values()), pages, True
        pages += 1
        new = 0
        for journey in page:
            key = (
                journey.departure,
                journey.arrival,
                tuple((leg.mode, leg.line, leg.start) for leg in journey.legs),
            )
            if key not in found:
                found[key] = journey
                new += 1
        if forward:
            later = [j.departure for j in page if j.departure >= cursor]
            if not new or not later:
                break
            cursor = max(later) + timedelta(minutes=1)
        else:
            earlier = [j.arrival for j in page if j.arrival <= cursor]
            if not new or not earlier:
                break
            cursor = min(earlier) - timedelta(minutes=1)
    return list(found.values()), pages, False


@operation(
    "search_journey",
    "Public-transport itinerary via MOTIS: arrive before, depart after, or both (a range). "
    "Distinguishes results, no candidate, and routing outage. Never invents a journey.",
    JourneyIn,
    mutates=False,
)
def search_journey(ctx: OpContext, body: JourneyIn) -> dict[str, Any]:
    if body.depart_after is None and body.arrive_before is None:
        raise OpError("indiquez au moins un départ minimal ou une arrivée maximale")
    destination = ctx.session.get(PlaceRow, body.destination_place_id)
    if destination is None:
        raise OpError("destination inconnue")
    if body.use_gps:
        position = get_position(ctx, NoInput())
        if not position.get("reliable_now"):
            return {
                "status": "origin_unknown",
                "detail": "GPS absent, périmé ou imprécis : demandez le point de départ",
            }
        origin = Coordinates(position["latitude"], position["longitude"])
    else:
        place = ctx.session.get(PlaceRow, body.origin_place_id) if body.origin_place_id else None
        if place is None:
            return {"status": "origin_unknown", "detail": "point de départ inconnu : demandez-le"}
        origin = Coordinates(place.latitude, place.longitude)
    if ctx.routing is None:
        return {"status": "unavailable", "detail": "aucun moteur de routage configuré"}
    lower = None if body.depart_after is None else _local(body.depart_after, ctx.tz)
    upper = None if body.arrive_before is None else _local(body.arrive_before, ctx.tz)
    if lower is not None and upper is not None and upper <= lower:
        raise OpError("plage horaire vide")
    target = Coordinates(destination.latitude, destination.longitude)
    try:
        found, pages, incomplete = _explore_journeys(ctx.routing, origin, target, lower, upper)
    except RoutingUnavailableError as exc:
        return {"status": "error", "detail": f"routage indisponible : {exc}"}
    candidates = [
        j
        for j in found
        if (lower is None or j.departure >= lower) and (upper is None or j.arrival <= upper)
    ]
    if not candidates:
        return {
            "status": "no_candidate",
            "explored": len(found),
            "pages": pages,
            "exploration_incomplete": incomplete,
            "detail": "aucun trajet dans la plage explorée"
            + (" (exploration interrompue par une panne de routage)" if incomplete else ""),
        }
    keys: dict[str, Callable[[Journey], Any]] = {
        "shortest": lambda j: (j.duration, j.arrival, j.departure),
        "earliest_arrival": lambda j: (j.arrival, j.duration),
        "latest_departure": lambda j: (-j.departure.timestamp(), j.duration),
    }
    ranked = sorted(candidates, key=keys[body.objective])
    return {
        "status": "ok",
        "objective": body.objective,
        "best": _journey_out(ranked[0]),
        "alternatives": [_journey_out(j) for j in ranked[1:4]],
        "explored": len(found),
        "pages": pages,
        "exploration_incomplete": incomplete,
        "note": "meilleur parmi les candidats effectivement explorés"
        + (" (exploration interrompue par une panne de routage)" if incomplete else ""),
    }


# --- execution ---


def tool_definitions() -> list[dict[str, Any]]:
    """Anthropic tool definitions (deterministic order: stable prompt cache prefix)."""
    return [
        {
            "name": op.name,
            "description": op.description,
            "input_schema": op.model.model_json_schema(),
        }
        for op in sorted(CATALOG.values(), key=lambda o: o.name)
    ]


def run_operation(ctx: OpContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate and run one operation inside a savepoint: a failure leaves nothing behind.

    Returns ``{"ok": True, "result": ...}`` or ``{"ok": False, "error": ...}``."""
    op = CATALOG.get(name)
    if op is None:
        return {"ok": False, "error": f"opération inconnue : {name}"}
    try:
        body = op.model.model_validate(arguments)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
        return {"ok": False, "error": f"arguments invalides : {details}"}
    savepoint = ctx.session.begin_nested()
    try:
        result = op.handler(ctx, body)
        ctx.session.flush()
    except OpError as exc:
        savepoint.rollback()
        return {"ok": False, "error": str(exc)}
    except DomainError as exc:
        savepoint.rollback()
        return {"ok": False, "error": str(exc)}
    except HTTPException as exc:
        savepoint.rollback()
        return {"ok": False, "error": str(exc.detail)}
    if savepoint.is_active:
        savepoint.commit()
    if op.mutates:
        ctx.changed.append(name)
        audit(ctx.session, ctx.actor, f"op:{name}", "operation", "-", fields=sorted(arguments))
        if op.replan:
            set_setting(ctx.session, "replan_requested", ctx.now.isoformat())
    return {"ok": True, "result": result}


def expand_all_series(session: Session, now: datetime) -> int:
    """Worker: keep recurring events materialised on the rolling horizon."""
    created = 0
    for series in session.scalars(
        select(RecurrenceSeriesRow).where(RecurrenceSeriesRow.ended_at.is_(None))
    ):
        try:
            created += expand_series(session, series, now)
        except DomainError:
            continue  # an invalid stored rule never blocks the other series
    return created
