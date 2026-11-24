"""Database session dependency and the phone position pushed by Home Assistant.

The position is telemetry, not a business change: it is the ONLY real location source
(ARCHITECTURE.md §2) and is never writable through the operation catalogue or the LLM.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from tempo.db.models import PhonePositionRow

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


class PositionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(pattern=r"^device_tracker\.[a-z0-9_]{1,200}$")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(ge=0, le=100_000)
    observed_at: AwareDatetime


@router.post("/location", status_code=status.HTTP_204_NO_CONTENT)
def push_location(body: PositionIn, session: SessionDep) -> Response:
    """Keep only the latest observation per tracker (an older one never overwrites)."""
    values = {**body.model_dump(), "received_at": datetime.now(UTC)}
    statement = insert(PhonePositionRow).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[PhonePositionRow.entity_id],
        set_={k: statement.excluded[k] for k in values if k != "entity_id"},
        where=statement.excluded.observed_at > PhonePositionRow.observed_at,
    )
    session.execute(statement)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
