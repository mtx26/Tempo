"""Request/response schemas of the private API used by the Home Assistant integration."""

import uuid
from datetime import date

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from tempo.domain.models import TargetKind


class TaskOut(BaseModel):
    id: uuid.UUID
    subject: str
    effort_minutes: int | None
    completed_minutes: int
    remaining_minutes: int | None
    target_date: date | None
    target_kind: TargetKind
    archived: bool


class PositionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(pattern=r"^device_tracker\.[a-z0-9_]{1,200}$")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(ge=0, le=100_000)
    observed_at: AwareDatetime
