"""Routes consumed by the Home Assistant integration (the only Tempo user interface)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from tempo.api.schemas import PositionIn, TaskCreate, TaskOut, TaskUpdate
from tempo.db.models import AuditEntry, PhonePositionRow, StudyTaskRow
from tempo.domain.ops import record_work, remaining_minutes

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


def _audit(
    session: Session, action: str, entity: str, entity_id: str, details: dict[str, Any]
) -> None:
    session.add(
        AuditEntry(
            at=datetime.now(UTC),
            actor=ACTOR,
            action=action,
            entity=entity,
            entity_id=entity_id,
            details=details,
        )
    )


@router.get("/tasks")
def list_tasks(session: SessionDep) -> list[TaskOut]:
    rows = session.scalars(
        select(StudyTaskRow).order_by(StudyTaskRow.created_at, StudyTaskRow.id)
    ).all()
    return [_out(r) for r in rows]


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
def create_task(body: TaskCreate, session: SessionDep) -> TaskOut:
    row = StudyTaskRow(**body.model_dump())
    session.add(row)
    session.flush()
    session.refresh(row)
    _audit(
        session,
        "task_created",
        "study_task",
        str(row.id),
        {"fields": sorted(body.model_fields_set)},
    )
    return _out(row)


@router.patch("/tasks/{task_id}")
def update_task(task_id: uuid.UUID, body: TaskUpdate, session: SessionDep) -> TaskOut:
    row = session.get(StudyTaskRow, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    done = changes.pop("completed_minutes", None)
    for name, value in changes.items():
        setattr(row, name, value)
    if done is not None and done != row.completed_minutes:
        # Corrections go through the work log (never overwrite the total silently).
        record_work(
            session,
            row,
            done - row.completed_minutes,
            now=datetime.now(UTC),
            note="correction",
        )
        changes["completed_minutes"] = done
    session.flush()
    _audit(session, "task_updated", "study_task", str(row.id), {"fields": sorted(changes)})
    return _out(row)


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
