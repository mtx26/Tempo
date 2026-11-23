"""Request/response schemas of the private API used by the Home Assistant integration."""

import uuid
from datetime import date

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from tempo.domain.models import TargetKind

MAX_EFFORT_MINUTES = 100_000


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    effort_minutes: int = Field(gt=0, le=MAX_EFFORT_MINUTES)
    target_date: date
    target_kind: TargetKind = TargetKind.SOFT
    """Soft by default (REQUIREMENTS); a hard deadline must be stated explicitly."""


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed_minutes: int | None = Field(default=None, ge=0, le=MAX_EFFORT_MINUTES)
    effort_minutes: int | None = Field(default=None, gt=0, le=MAX_EFFORT_MINUTES)
    target_date: date | None = None
    target_kind: TargetKind | None = None
    archived: bool | None = None


class TaskOut(BaseModel):
    id: uuid.UUID
    subject: str
    effort_minutes: int
    completed_minutes: int
    remaining_minutes: int
    target_date: date
    target_kind: TargetKind
    archived: bool


class PositionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(pattern=r"^device_tracker\.[a-z0-9_]{1,200}$")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(ge=0, le=100_000)
    observed_at: AwareDatetime
