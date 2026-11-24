"""Routes consumed by the Home Assistant integration: passive task list and the phone's real
GPS position. Business changes go through the conversation (``/v1/conversation``)."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from tempo.api.schemas import PositionIn, TaskOut
from tempo.db.models import PhonePositionRow, StudyTaskRow
from tempo.domain.ops import remaining_minutes

ACTOR = "home_assistant"
router = APIRouter(prefix="/v1")


def get_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] | None = getattr(request.app.state, "sessions", None)
    if factory is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database not configured")
    with factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


# scope="function": commit BEFORE the response is sent, so a 2xx always means the write is
# durable and visible to the next request (default "request" scope commits afterwards).
SessionDep = Annotated[Session, Depends(get_session, scope="function")]


def _out(row: StudyTaskRow) -> TaskOut:
    return TaskOut(
        id=row.id,
        subject=row.subject,
        effort_minutes=row.effort_minutes,
        completed_minutes=row.completed_minutes,
        remaining_minutes=remaining_minutes(row),
        target_date=row.target_date,
        target_kind=row.target_kind,
        archived=row.archived,
    )


@router.get("/tasks")
def list_tasks(session: SessionDep) -> list[TaskOut]:
    rows = session.scalars(
        select(StudyTaskRow).order_by(StudyTaskRow.created_at, StudyTaskRow.id)
    ).all()
    return [_out(r) for r in rows]


@router.post("/location", status_code=status.HTTP_204_NO_CONTENT)
def push_location(body: PositionIn, session: SessionDep) -> Response:
    values = {**body.model_dump(), "received_at": datetime.now(UTC)}
    statement = insert(PhonePositionRow).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[PhonePositionRow.entity_id],
        set_={k: statement.excluded[k] for k in values if k != "entity_id"},
        where=statement.excluded.observed_at > PhonePositionRow.observed_at,
    )
    session.execute(statement)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
