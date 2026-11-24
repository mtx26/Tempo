"""Typed runtime configuration loaded from environment variables (prefix ``TEMPO_``).

Secrets are typed ``SecretStr`` so they never appear in ``repr`` or logs.
"""

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
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

    api_token_previous: SecretStr | None = Field(default=None, min_length=MIN_TOKEN_LENGTH)
    """Previous token, still accepted during a rotation (Docker secret
    ``tempo_api_token_previous``). Remove it once Home Assistant uses the new token."""

    database_url: SecretStr | None = None
    """SQLAlchemy URL of PostgreSQL (e.g. from the Docker secret ``tempo_database_url``)."""

    anthropic_api_key: SecretStr | None = None
    """Optional (Docker secret ``tempo_anthropic_api_key``): enables AI interpretation of text
    the deterministic parser does not understand. Tempo works fully without it."""

    motis_url: str | None = None
    """MOTIS base URL: lets the assistant search journeys and re-plan synchronously."""

    timezone: str = "Europe/Brussels"
    """IANA timezone used to interpret local wall-clock planning rules."""

    @field_validator("api_token", "api_token_previous")
    @classmethod
    def _non_trivial_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if any(char.isspace() for char in raw):
            raise ValueError("token must not contain whitespace")
        if len(set(raw)) < MIN_DISTINCT_TOKEN_CHARS:
            raise ValueError("token is too predictable; use secrets.token_urlsafe(32)")
        return value

    @model_validator(mode="after")
    def _distinct_tokens(self) -> "Settings":
        previous = self.api_token_previous
        if (
            previous is not None
            and previous.get_secret_value() == self.api_token.get_secret_value()
        ):
            raise ValueError("api_token_previous must differ from api_token")
        return self

    def accepted_tokens(self) -> tuple[bytes, ...]:
        tokens = [self.api_token]
        if self.api_token_previous is not None:
            tokens.append(self.api_token_previous)
        return tuple(t.get_secret_value().encode() for t in tokens)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value
