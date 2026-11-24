"""Read routes for Home Assistant's passive display (calendar, sensors, todo), the technical
settings of the integration's configuration screens, and notification acknowledgement.

Every route needs the bearer token (mounted with ``require_token`` in ``api.app``). Business
changes never go through these routes: they go through the conversation and the operation
catalogue (``/v1/conversation``, ``/v1/operations/{name}``).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import AwareDatetime
from sqlalchemy import select

from tempo.api.ha import SessionDep
from tempo.assistant.catalog import activity_out
from tempo.db.models import ActivityRow, NotificationRow, PlaceAliasRow, PlaceRow, ProposalRow
from tempo.domain.ops import DomainError
from tempo.planning.proposals import proposal_out
from tempo.planning.settings import SettingsIn, apply_settings, current_settings
from tempo.planning.views import schedule_items, summary

ACTOR = "home_assistant"
router = APIRouter(prefix="/v1")
MAX_RANGE = timedelta(days=62)


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
    return summary(session, datetime.now(UTC), request.app.state.timezone)


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


@router.get("/settings")
def get_settings(session: SessionDep) -> dict[str, Any]:
    return current_settings(session)


@router.patch("/settings", status_code=status.HTTP_204_NO_CONTENT)
def patch_settings(body: SettingsIn, session: SessionDep) -> Response:
    """Technical configuration from the integration's screens (home place, phone tracker)."""
    try:
        apply_settings(session, body, actor=ACTOR)
    except DomainError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/activities")
def list_activities(session: SessionDep) -> list[dict[str, Any]]:
    rows = session.scalars(select(ActivityRow).order_by(ActivityRow.earliest_start_at))
    return [activity_out(r) for r in rows]


@router.get("/proposals")
def list_proposals(
    session: SessionDep, state: Annotated[str, Query(alias="status")] = "pending"
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(ProposalRow).where(ProposalRow.status == state).order_by(ProposalRow.created_at)
    )
    return [proposal_out(r) for r in rows]


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
    """Home Assistant dismissed the persistent notification (no business effect)."""
    row = session.get(NotificationRow, notification_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
    if row.acknowledged_at is None:
        row.acknowledged_at = datetime.now(UTC)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
