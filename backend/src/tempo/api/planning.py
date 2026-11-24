"""Planning routes for Home Assistant: calendar, sensors, configuration, approvals.

Every route needs the bearer token (mounted with ``require_token`` in ``api.app``). Writes are
audited. Re-planning is asynchronous: ``POST /v1/replan`` records a request that the worker
handles within a minute (the solver may take several seconds; HA must not wait on it).
"""

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tempo.api.ha import SessionDep
from tempo.db.models import (
    ActivityRow,
    AvailabilityRow,
    NotificationRow,
    PlaceAliasRow,
    PlaceRow,
    ProposalRow,
    StudyBlockRow,
)
from tempo.domain.ops import complete_block
from tempo.planning.service import audit, get_setting, proposal_revision, set_setting
from tempo.planning.views import schedule_items, summary

ACTOR = "home_assistant"
router = APIRouter(prefix="/v1")
MAX_RANGE = timedelta(days=62)
MAX_ACTIVITY_AHEAD = timedelta(days=60)
MAX_WINDOW = timedelta(days=14)


def _tz(request: Request) -> Any:
    return request.app.state.timezone


# --- calendar & sensors -------------------------------------------------------------------------


@router.get("/schedule")
def get_schedule(
    session: SessionDep,
    start: Annotated[AwareDatetime, Query()],
    end: Annotated[AwareDatetime, Query()],
) -> list[dict[str, Any]]:
    if end <= start or end - start > MAX_RANGE:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid range (max 62 days)")
    return schedule_items(session, start, end)


@router.get("/summary")
def get_summary(session: SessionDep, request: Request) -> dict[str, Any]:
    return summary(session, datetime.now(UTC), _tz(request))


@router.post("/replan", status_code=status.HTTP_202_ACCEPTED)
def request_replan(session: SessionDep) -> dict[str, str]:
    when = datetime.now(UTC).isoformat()
    set_setting(session, "replan_requested", when)
    audit(session, ACTOR, "replan_requested", "schedule", "-")
    return {"requested_at": when}


# --- places & availability ----------------------------------------------------------------------

SLUG = r"^[a-z0-9_]{1,50}$"


class PlaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    aliases: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=50
    )


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


@router.get("/places")
def list_places(session: SessionDep) -> list[dict[str, Any]]:
    aliases: dict[str, list[str]] = {}
    for alias in session.scalars(select(PlaceAliasRow)):
        aliases.setdefault(alias.place_id, []).append(alias.alias)
    return [
        {
            "id": p.id,
            "name": p.name,
            "latitude": p.latitude,
            "longitude": p.longitude,
            "aliases": sorted(aliases.get(p.id, [])),
        }
        for p in session.scalars(select(PlaceRow).order_by(PlaceRow.id))
    ]


@router.put("/places/{place_id}", status_code=status.HTTP_204_NO_CONTENT)
def put_place(place_id: str, body: PlaceIn, session: SessionDep) -> Response:
    if not re.match(SLUG, place_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "place id must match " + SLUG)
    row = session.get(PlaceRow, place_id) or PlaceRow(id=place_id)
    row.name, row.latitude, row.longitude = body.name, body.latitude, body.longitude
    session.add(row)
    session.flush()
    session.execute(delete(PlaceAliasRow).where(PlaceAliasRow.place_id == place_id))
    for alias in {_norm(a) for a in body.aliases}:
        existing = session.get(PlaceAliasRow, alias)
        if existing is not None and existing.place_id != place_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"alias already used by {existing.place_id}"
            )
        session.add(PlaceAliasRow(alias=alias, place_id=place_id))
    audit(session, ACTOR, "place_saved", "place", place_id, aliases=len(body.aliases))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/places/{place_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_place(place_id: str, session: SessionDep) -> Response:
    if get_setting(session, "home_place_id") == place_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "this place is the configured home")
    row = session.get(PlaceRow, place_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "place not found")
    session.delete(row)
    audit(session, ACTOR, "place_deleted", "place", place_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class WindowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekday: int = Field(ge=0, le=6)
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(([01]\d|2[0-3]):[0-5]\d|24:00)$")
    place_id: str

    def minutes(self) -> tuple[int, int]:
        def conv(text: str) -> int:
            hours, minutes = text.split(":")
            return int(hours) * 60 + int(minutes)

        return conv(self.start), conv(self.end)


@router.get("/availability")
def get_availability(session: SessionDep) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(AvailabilityRow).order_by(AvailabilityRow.weekday, AvailabilityRow.start_minute)
    )
    return [
        {
            "weekday": r.weekday,
            "start": f"{r.start_minute // 60:02d}:{r.start_minute % 60:02d}",
            "end": f"{r.end_minute // 60:02d}:{r.end_minute % 60:02d}",
            "place_id": r.place_id,
        }
        for r in rows
    ]


@router.put("/availability", status_code=status.HTTP_204_NO_CONTENT)
def put_availability(body: list[WindowIn], session: SessionDep) -> Response:
    if len(body) > 200:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "too many windows")
    for window in body:
        start, end = window.minutes()
        if end <= start:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "window end before start")
        if session.get(PlaceRow, window.place_id) is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown place {window.place_id}"
            )
    session.execute(delete(AvailabilityRow))
    for window in body:
        start, end = window.minutes()
        session.add(
            AvailabilityRow(
                weekday=window.weekday, start_minute=start, end_minute=end, place_id=window.place_id
            )
        )
    audit(session, ACTOR, "availability_replaced", "availability_window", "-", windows=len(body))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- settings -----------------------------------------------------------------------------------


class StayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    place_id: str
    start: AwareDatetime
    end: AwareDatetime


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    home_place_id: str | None = None
    tracker_entity_id: str | None = Field(
        default=None, pattern=r"^device_tracker\.[a-z0-9_]{1,200}$"
    )
    horizon_days: int | None = Field(default=None, ge=1, le=42)
    buffer_minutes: int | None = Field(default=None, ge=0, le=120)
    stays: list[StayIn] | None = Field(default=None, max_length=50)
    study_preferences: dict[str, int] | None = None
    reminder_minutes: list[Annotated[int, Field(ge=0, le=240)]] | None = Field(
        default=None, max_length=5
    )
    """Departure reminders before the advised leave time (default [60, 15])."""


SETTING_KEYS = (
    "home_place_id",
    "tracker_entity_id",
    "horizon_days",
    "buffer_minutes",
    "stays",
    "study_preferences",
    "reminder_minutes",
)


@router.get("/settings")
def get_settings(session: SessionDep) -> dict[str, Any]:
    return {key: get_setting(session, key) for key in SETTING_KEYS}


@router.patch("/settings", status_code=status.HTTP_204_NO_CONTENT)
def patch_settings(body: SettingsIn, session: SessionDep) -> Response:
    changes = body.model_dump(mode="json", exclude_unset=True)
    for place_key in ("home_place_id",):
        if changes.get(place_key) and session.get(PlaceRow, changes[place_key]) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown place")
    for stay in changes.get("stays") or []:
        if session.get(PlaceRow, stay["place_id"]) is None or stay["end"] <= stay["start"]:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid stay")
    if changes.get("study_preferences") is not None:
        from tempo.scheduler.model import Preferences

        try:
            Preferences(**changes["study_preferences"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid study preferences"
            ) from exc
    for key, value in changes.items():
        set_setting(session, key, value)
    audit(session, ACTOR, "settings_changed", "app_setting", "-", keys=sorted(changes))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- activities (trainings & races) -------------------------------------------------------------


class ActivityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(training|race)$")
    title: str = Field(min_length=1, max_length=300)
    notes: str = Field(default="", max_length=10_000)
    duration_minutes: int = Field(gt=0, le=24 * 60)
    start: AwareDatetime | None = None
    """Race: its fixed start. Training: optional preferred start."""
    earliest_start: AwareDatetime | None = None
    latest_end: AwareDatetime | None = None
    place_id: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "ActivityIn":
        if self.kind == "race" and self.start is None:
            raise ValueError("a race needs its fixed start")
        if (
            self.kind == "training"
            and self.start is None
            and (self.earliest_start is None or self.latest_end is None)
        ):
            raise ValueError("a training needs a window (earliest_start, latest_end) or a start")
        return self

    def window(self) -> tuple[datetime, datetime]:
        duration = timedelta(minutes=self.duration_minutes)
        if self.kind == "race" and self.start is not None:
            return self.start, self.start + duration
        if self.earliest_start is not None and self.latest_end is not None:
            return self.earliest_start, self.latest_end
        if self.start is None:  # excluded by the validator
            raise ValueError("training without window or start")
        day = self.start.date()
        # Default flexibility: the same day +/- 1 day.
        return (
            datetime.combine(
                day - timedelta(days=1), datetime.min.time(), tzinfo=self.start.tzinfo
            ),
            datetime.combine(
                day + timedelta(days=2), datetime.min.time(), tzinfo=self.start.tzinfo
            ),
        )


def _activity_out(row: ActivityRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "title": row.title,
        "notes": row.notes,
        "duration_minutes": row.duration_minutes,
        "earliest_start": row.earliest_start_at.isoformat(),
        "latest_end": row.latest_end_at.isoformat(),
        "preferred_start": None
        if row.preferred_start_at is None
        else row.preferred_start_at.isoformat(),
        "planned_start": None if row.planned_start_at is None else row.planned_start_at.isoformat(),
        "place_id": row.place_id,
        "locked": row.locked,
        "status": row.status,
        "source": row.source,
        "preferred_day": None if row.preferred_day is None else row.preferred_day.isoformat(),
        "user_modified": row.user_modified,
    }


@router.get("/activities")
def list_activities(session: SessionDep) -> list[dict[str, Any]]:
    rows = session.scalars(select(ActivityRow).order_by(ActivityRow.earliest_start_at))
    return [_activity_out(r) for r in rows]


@router.post("/activities", status_code=status.HTTP_201_CREATED)
def create_activity(body: ActivityIn, session: SessionDep) -> dict[str, Any]:
    if body.place_id is not None and session.get(PlaceRow, body.place_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown place")
    earliest, latest = body.window()
    if latest - earliest < timedelta(minutes=body.duration_minutes):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "window shorter than duration")
    now = datetime.now(UTC)
    if latest <= now or earliest > now + MAX_ACTIVITY_AHEAD or latest - earliest > MAX_WINDOW:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "activity must end in the future, start within 60 days, window at most 14 days",
        )
    row = ActivityRow(
        kind=body.kind,
        title=body.title,
        notes=body.notes,
        duration_minutes=body.duration_minutes,
        earliest_start_at=earliest,
        latest_end_at=latest,
        preferred_start_at=body.start if body.kind == "training" else None,
        place_id=body.place_id,
    )
    session.add(row)
    session.flush()
    audit(session, ACTOR, "activity_created", "activity", str(row.id), kind=body.kind)
    set_setting(session, "replan_requested", datetime.now(UTC).isoformat())
    return _activity_out(row)


class ActivityPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locked: bool | None = None
    status: str | None = Field(default=None, pattern="^(active|cancelled|done)$")
    notes: str | None = Field(default=None, max_length=10_000)


@router.patch("/activities/{activity_id}")
def patch_activity(
    activity_id: uuid.UUID, body: ActivityPatch, session: SessionDep
) -> dict[str, Any]:
    row = session.get(ActivityRow, activity_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "activity not found")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if changes.get("locked") and row.kind == "training" and row.planned_start_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "training not placed yet: nothing to lock")
    if changes.get("status") == "active" and row.status == "to_confirm":
        # Duration (and race start) must go through /confirm: never activated by a PATCH.
        raise HTTPException(status.HTTP_409_CONFLICT, "use /confirm for activities to confirm")
    for key, value in changes.items():
        setattr(row, key, value)
    if changes:
        row.user_modified = True  # a re-sent coach plan will not overwrite it silently
    audit(session, ACTOR, "activity_updated", "activity", str(row.id), fields=sorted(changes))
    set_setting(session, "replan_requested", datetime.now(UTC).isoformat())
    return _activity_out(row)


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duration_minutes: int = Field(gt=0, le=24 * 60)
    start: AwareDatetime | None = None
    """Required for a race (fixed start); must be on the race day."""


@router.post("/activities/{activity_id}/confirm")
def confirm_activity(
    activity_id: uuid.UUID, body: ConfirmIn, session: SessionDep
) -> dict[str, Any]:
    """Give the duration (and race start) the coach's plan did not state: never guessed."""
    row = session.get(ActivityRow, activity_id, with_for_update=True)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "activity not found")
    if row.status != "to_confirm":
        raise HTTPException(status.HTTP_409_CONFLICT, f"activity is {row.status}")
    duration = timedelta(minutes=body.duration_minutes)
    if row.kind == "race":
        if body.start is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "a race needs its start")
        if not row.earliest_start_at <= body.start < row.latest_end_at:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "start must be on the race day"
            )
        row.earliest_start_at, row.latest_end_at = body.start, body.start + duration
    elif row.latest_end_at - row.earliest_start_at < duration:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "longer than its window")
    row.duration_minutes = body.duration_minutes
    row.status = "active"
    row.user_modified = True
    audit(session, ACTOR, "activity_confirmed", "activity", str(row.id))
    set_setting(session, "replan_requested", datetime.now(UTC).isoformat())
    return _activity_out(row)


# --- study blocks -------------------------------------------------------------------------------


def _block(session: Session, block_id: uuid.UUID) -> StudyBlockRow:
    row = session.get(StudyBlockRow, block_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "block not found")
    return row


@router.post("/blocks/{block_id}/lock", status_code=status.HTTP_204_NO_CONTENT)
def lock_block(block_id: uuid.UUID, session: SessionDep) -> Response:
    """The user explicitly protects a block: it will never be moved without approval."""
    row = _block(session, block_id)
    if not row.locked and row.state not in ("promoted", "done"):
        session.add(
            StudyBlockRow(
                task_id=row.task_id, start_at=row.start_at, end_at=row.end_at, locked=True
            )
        )
        row.state = "promoted"
    audit(session, ACTOR, "block_locked", "study_block", str(block_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/blocks/{block_id}/unlock", status_code=status.HTTP_204_NO_CONTENT)
def unlock_block(block_id: uuid.UUID, session: SessionDep) -> Response:
    row = _block(session, block_id)
    if row.locked:
        session.delete(row)
        set_setting(session, "replan_requested", datetime.now(UTC).isoformat())
    audit(session, ACTOR, "block_unlocked", "study_block", str(block_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/blocks/{block_id}/done", status_code=status.HTTP_204_NO_CONTENT)
def block_done(block_id: uuid.UUID, session: SessionDep) -> Response:
    row = _block(session, block_id)
    if row.state not in ("done", "partial"):
        complete_block(session, row, datetime.now(UTC), None)
        row.locked = False
    audit(session, ACTOR, "block_done", "study_block", str(block_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- approvals & notifications ------------------------------------------------------------------


def _proposal_out(row: ProposalRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "title": row.title,
        "details": row.details,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/proposals")
def list_proposals(
    session: SessionDep, state: Annotated[str, Query(alias="status")] = "pending"
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(ProposalRow).where(ProposalRow.status == state).order_by(ProposalRow.created_at)
    )
    return [_proposal_out(r) for r in rows]


def _apply_unlock(session: Session, row: ProposalRow) -> None:
    block = session.get(StudyBlockRow, uuid.UUID(row.details["block_id"]))
    if block is not None and block.locked:
        session.delete(block)


def _apply_unlock_activity(session: Session, row: ProposalRow) -> None:
    activity = session.get(ActivityRow, uuid.UUID(row.details["activity_id"]))
    if activity is not None and activity.locked:
        activity.locked = False  # flexible again: the next re-plan moves it around the class


PROPOSAL_HANDLERS: dict[str, Callable[[Session, ProposalRow], None]] = {
    "unlock_block": _apply_unlock,
    "unlock_activity": _apply_unlock_activity,
}
"""What approving a proposal does; other modules (e.g. training e-mails) register theirs."""


def _decide(session: Session, proposal_id: uuid.UUID, approve: bool) -> dict[str, Any]:
    row = session.get(ProposalRow, proposal_id, with_for_update=True)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
    if row.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"proposal already {row.status}")
    if (
        approve
        and row.input_revision is not None
        and proposal_revision(session, row.details) != row.input_revision
    ):
        # Inputs changed since the proposal was computed: never apply a stale decision.
        row.status, row.decided_at = "obsolete", datetime.now(UTC)
        set_setting(session, "replan_requested", datetime.now(UTC).isoformat())
        audit(session, ACTOR, "proposal_obsolete", "proposal", str(row.id), kind=row.kind)
        return _proposal_out(row)
    row.status, row.decided_at = ("approved" if approve else "rejected"), datetime.now(UTC)
    if approve:
        handler = PROPOSAL_HANDLERS.get(row.kind)
        if handler is not None:
            handler(session, row)
        set_setting(session, "replan_requested", datetime.now(UTC).isoformat())
    audit(session, ACTOR, "proposal_" + row.status, "proposal", str(row.id), kind=row.kind)
    return _proposal_out(row)


@router.post("/proposals/{proposal_id}/approve")
def approve(proposal_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    return _decide(session, proposal_id, True)


@router.post("/proposals/{proposal_id}/reject")
def reject(proposal_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    return _decide(session, proposal_id, False)


@router.get("/notifications")
def list_notifications(
    session: SessionDep, unread: bool = True, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[dict[str, Any]]:
    query = select(NotificationRow).order_by(NotificationRow.created_at.desc()).limit(limit)
    if unread:
        query = query.where(NotificationRow.acknowledged_at.is_(None))
    return [
        {
            "id": str(n.id),
            "kind": n.kind,
            "title": n.title,
            "message": n.message,
            "created_at": n.created_at.isoformat(),
            "acknowledged": n.acknowledged_at is not None,
        }
        for n in session.scalars(query)
    ]


@router.post("/notifications/{notification_id}/ack", status_code=status.HTTP_204_NO_CONTENT)
def ack_notification(notification_id: uuid.UUID, session: SessionDep) -> Response:
    row = session.get(NotificationRow, notification_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
    if row.acknowledged_at is None:
        row.acknowledged_at = datetime.now(UTC)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
