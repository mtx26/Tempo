"""Typed runtime configuration loaded from environment variables (prefix ``TEMPO_``).

Secrets are typed ``SecretStr`` so they never appear in ``repr`` or logs.
"""

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_TOKEN_LENGTH = 32
MIN_DISTINCT_TOKEN_CHARS = 16
DOCKER_SECRETS_DIR = Path("/run/secrets")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TEMPO_",
        extra="ignore",
        hide_input_in_errors=True,
        # Docker/Swarm secrets (file ``/run/secrets/tempo_api_token``) take effect when mounted.
        secrets_dir=DOCKER_SECRETS_DIR if DOCKER_SECRETS_DIR.is_dir() else None,
    )

    api_token: SecretStr = Field(min_length=MIN_TOKEN_LENGTH)
    """Shared service token used by the Home Assistant integration (Bearer auth)."""

    timezone: str = "Europe/Brussels"
    """IANA timezone used to interpret local wall-clock planning rules."""

    @field_validator("api_token")
    @classmethod
    def _non_trivial_token(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if any(char.isspace() for char in raw):
            raise ValueError("token must not contain whitespace")
        if len(set(raw)) < MIN_DISTINCT_TOKEN_CHARS:
            raise ValueError("token is too predictable; use secrets.token_urlsafe(32)")
        return value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value
