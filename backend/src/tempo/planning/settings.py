"""Permanent settings (home place, phone tracker, horizon, margins, stays, preferences),
validated once for both the conversation (``update_settings``) and the technical
configuration of the Home Assistant integration (``PATCH /v1/settings``)."""

from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from tempo.db.models import PlaceRow
from tempo.domain.ops import DomainError
from tempo.planning.service import audit, get_setting, set_setting
from tempo.scheduler.model import Preferences


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


SETTING_KEYS = tuple(SettingsIn.model_fields)


def current_settings(session: Session) -> dict[str, Any]:
    return {key: get_setting(session, key) for key in SETTING_KEYS}


def apply_settings(session: Session, body: SettingsIn, *, actor: str) -> list[str]:
    """Validate then store the given settings; returns the changed keys."""
    changes = body.model_dump(mode="json", exclude_unset=True)
    if changes.get("home_place_id") and session.get(PlaceRow, changes["home_place_id"]) is None:
        raise DomainError("lieu inconnu")
    for stay in changes.get("stays") or []:
        if session.get(PlaceRow, stay["place_id"]) is None or stay["end"] <= stay["start"]:
            raise DomainError("séjour invalide")
    if changes.get("study_preferences") is not None:
        try:
            Preferences(**changes["study_preferences"])
        except (TypeError, ValueError) as exc:
            raise DomainError("préférences d'étude invalides") from exc
    for key, value in changes.items():
        set_setting(session, key, value)
    audit(session, actor, "settings_changed", "app_setting", "-", keys=sorted(changes))
    return sorted(changes)
