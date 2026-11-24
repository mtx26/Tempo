"""Read models for Home Assistant: the merged calendar and the sensor summary."""

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from tempo.db.models import (
    ActivityRow,
    FixedEventRow,
    NotificationRow,
    PlaceRow,
    ProposalRow,
    StudyBlockRow,
    StudyTaskRow,
)
from tempo.domain.models import EventStatus
from tempo.domain.ops import remaining_minutes
from tempo.planning.service import active_version, get_setting, place_directory
from tempo.scheduler.model import utc


def _iso(value: datetime) -> str:
    return value.isoformat()


def schedule_items(session: Session, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Every planned item overlapping ``[start, end)``, sorted by start."""
    items: list[dict[str, Any]] = []
    directory = place_directory(session)
    for event in session.scalars(
        select(FixedEventRow).where(
            FixedEventRow.deleted_at.is_(None),
            FixedEventRow.status != EventStatus.CANCELLED,
            FixedEventRow.end_at > start,
            FixedEventRow.start_at < end,
        )
    ):
        place = directory.resolve(event.location)
        items.append(
            {
                "type": "class",
                "id": f"event:{event.id}",
                "title": event.summary,
                "start": _iso(event.start_at),
                "end": _iso(event.end_at),
                "location": event.location,
                "place_id": None if place is None else place.id,
                "fixed": True,
            }
        )
    subjects = {t.id: t.subject for t in session.scalars(select(StudyTaskRow))}
    version = active_version(session)
    block_filter: ColumnElement[bool] = StudyBlockRow.locked.is_(True)
    if version is not None:
        block_filter = or_(block_filter, StudyBlockRow.schedule_version_id == version.id)
    for block in session.scalars(
        select(StudyBlockRow).where(
            block_filter,
            StudyBlockRow.state != "done",
            StudyBlockRow.end_at > start,
            StudyBlockRow.start_at < end,
        )
    ):
        items.append(
            {
                "type": "study",
                "id": f"block:{block.id}",
                "block_id": str(block.id),
                "task_id": str(block.task_id),
                "title": f"Étude : {subjects.get(block.task_id, '?')}",
                "start": _iso(block.start_at),
                "end": _iso(block.end_at),
                "locked": block.locked,
                "state": block.state,
                "fixed": False,
            }
        )
    places = {p.id: p.name for p in session.scalars(select(PlaceRow))}
    for act in session.scalars(select(ActivityRow).where(ActivityRow.status == "active")):
        begins = act.earliest_start_at if act.scheduling == "fixed" else act.planned_start_at
        if begins is None or act.duration_minutes is None:
            continue
        ends = begins + timedelta(minutes=act.duration_minutes)
        if utc(ends) <= utc(start) or utc(begins) >= utc(end):
            continue
        items.append(
            {
                "type": act.kind,
                "id": f"activity:{act.id}",
                "activity_id": str(act.id),
                "title": act.title,
                "notes": act.notes,
                "start": _iso(begins),
                "end": _iso(ends),
                "location": places.get(act.place_id or ""),
                "place_id": act.place_id,
                "locked": act.locked,
                "fixed": act.scheduling == "fixed" or act.locked,
                "all_day": act.all_day,
                "busy": act.busy,
                "series_id": None if act.series_id is None else str(act.series_id),
                "task_id": None if act.task_id is None else str(act.task_id),
            }
        )
    if version is not None:
        for index, trip in enumerate(version.summary.get("travel", [])):
            t_start, t_end = (
                datetime.fromisoformat(trip["start"]),
                datetime.fromisoformat(trip["end"]),
            )
            if t_end <= t_start or utc(t_end) <= utc(start) or utc(t_start) >= utc(end):
                continue
            items.append(
                {
                    "type": "travel",
                    "id": f"travel:{version.id}:{index}",
                    "title": f"Trajet {trip['origin_name']} → {trip['destination_name']}",
                    "start": trip["start"],
                    "end": trip["end"],
                    "status": trip["status"],
                    "departure": trip.get("departure"),
                    "arrival": trip.get("arrival"),
                    "legs": trip.get("legs", []),
                    "origin": trip["origin"],
                    "destination": trip["destination"],
                    "fixed": False,
                }
            )
    items.sort(key=lambda i: utc(datetime.fromisoformat(i["start"])))
    return items


def summary(session: Session, now: datetime, tz: ZoneInfo) -> dict[str, Any]:
    today = now.astimezone(tz).date()
    day_start = datetime.combine(today, time(0), tzinfo=tz)
    upcoming = schedule_items(session, now, now + timedelta(days=7))
    next_event = next(
        (
            i
            for i in upcoming
            if i["type"] != "travel" and utc(datetime.fromisoformat(i["end"])) > utc(now)
        ),
        None,
    )
    next_departure = next(
        (
            i
            for i in upcoming
            if i["type"] == "travel"
            and utc(datetime.fromisoformat(i["departure"] or i["start"]))
            >= utc(now) - timedelta(minutes=5)
        ),
        None,
    )
    today_items = schedule_items(session, day_start, day_start + timedelta(days=1))
    today_study = sum(
        (datetime.fromisoformat(i["end"]) - datetime.fromisoformat(i["start"])).total_seconds()
        // 60
        for i in today_items
        if i["type"] == "study"
    )
    remaining = sum(
        remaining_minutes(t) or 0
        for t in session.scalars(select(StudyTaskRow).where(StudyTaskRow.archived.is_(False)))
    )
    version = active_version(session)
    plan = version.summary if version is not None else {}
    pending = session.scalar(
        select(func.count()).select_from(ProposalRow).where(ProposalRow.status == "pending")
    )
    unread = session.scalar(
        select(func.count())
        .select_from(NotificationRow)
        .where(NotificationRow.acknowledged_at.is_(None))
    )
    to_confirm = [
        {
            "id": str(a.id),
            "kind": a.kind,
            "title": a.title,
            "preferred_day": None if a.preferred_day is None else a.preferred_day.isoformat(),
        }
        for a in session.scalars(
            select(ActivityRow)
            .where(ActivityRow.status == "to_confirm")
            .order_by(ActivityRow.earliest_start_at)
        )
    ]
    return {
        "now": now.isoformat(),
        "plan_status": plan.get("status", "none"),
        "planned_at": plan.get("planned_at"),
        "issues": plan.get("issues", []),
        "warnings": plan.get("warnings", []),
        "unplanned_minutes": sum(plan.get("unplanned_minutes", {}).values()),
        "missed_blocks": plan.get("missed_blocks", 0),
        "next_event": next_event,
        "next_departure": next_departure,
        "departure_check": get_setting(session, "departure_check"),
        "remaining_study_minutes": remaining,
        "today_study_minutes": int(today_study),
        "pending_proposals": pending or 0,
        "unread_notifications": unread or 0,
        "to_confirm": to_confirm,
    }
