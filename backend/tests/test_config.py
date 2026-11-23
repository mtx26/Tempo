from pathlib import Path

import pytest
from pydantic import ValidationError

from tempo.config import Settings

VALID_TOKEN = "Zq3vK8pL2mN7xR4tY9wB6cF1hJ5sD0gA"


def test_reads_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPO_API_TOKEN", VALID_TOKEN)
    settings = Settings()
    assert settings.api_token.get_secret_value() == VALID_TOKEN


def test_default_timezone_is_brussels() -> None:
    assert Settings(api_token=VALID_TOKEN).timezone == "Europe/Brussels"


def test_missing_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPO_API_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_short_token_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(api_token="too-short")


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(api_token=VALID_TOKEN, timezone="Mars/Olympus")


def test_token_is_not_leaked_in_repr() -> None:
    assert VALID_TOKEN not in repr(Settings(api_token=VALID_TOKEN))


def test_invalid_token_value_is_not_echoed_in_errors() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(api_token="leaky-short")
    assert "leaky-short" not in str(exc_info.value)


@pytest.mark.parametrize("token", [" " * 40, "a" * 40, "ab" * 20, "abc def " * 5])
def test_trivial_or_whitespace_tokens_are_rejected(token: str) -> None:
    with pytest.raises(ValidationError):
        Settings(api_token=token)


def test_token_can_be_read_from_docker_secret_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEMPO_API_TOKEN", raising=False)
    secret = tmp_path / "tempo_api_token"
    secret.write_text(VALID_TOKEN + "\n")
    settings = Settings(_secrets_dir=str(tmp_path))
    assert settings.api_token.get_secret_value() == VALID_TOKEN


def test_secret_not_in_json_dump() -> None:
    assert VALID_TOKEN not in Settings(api_token=VALID_TOKEN).model_dump_json()
