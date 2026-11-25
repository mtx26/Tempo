"""LLM access: provider choice, request shape and failure handling (SDK replaced, no network)."""

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from tempo.ai.llm import (
    FALLBACK_BETA,
    MAX_INPUT_CHARS,
    ClaudeInterpreter,
    Extraction,
    LlmClient,
    build_interpreter,
    build_llm_client,
)
from tempo.config import LlmSettings

TODAY = date(2026, 12, 7)


def settings(**values: Any) -> LlmSettings:
    return LlmSettings(_env_file=None, **values)


def interpreter_with(
    behaviour: Any, *, server_fallbacks: bool = True
) -> tuple[ClaudeInterpreter, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def parse(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return behaviour(kwargs)

    llm = LlmClient(SimpleNamespace(parse=parse), "test", server_fallbacks=server_fallbacks)
    return ClaudeInterpreter(llm, model="claude-sonnet-5-5"), calls


def test_default_model_is_sonnet_and_no_key_means_no_llm() -> None:
    assert settings().llm_model == "claude-sonnet-5-5"
    assert build_interpreter(settings()) is None
    keyed = build_llm_client(settings(anthropic_api_key=SecretStr("sk-test")), timeout=5)
    assert keyed is not None and keyed.provider == "anthropic" and keyed.server_fallbacks


def test_vertex_needs_a_project_and_a_credentials_file(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    assert build_llm_client(settings(llm_provider="vertex"), timeout=5) is None
    incomplete = settings(
        llm_provider="vertex", vertex_project_id="tempo-project", vertex_credentials_file=missing
    )
    assert build_llm_client(incomplete, timeout=5) is None


def test_vertex_client_has_no_server_fallback(tmp_path: Path) -> None:
    """Vertex AI rejects ``fallbacks``: never sent; a refusal takes the deterministic path."""
    key = tmp_path / "sa.json"
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    key.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "tempo-project",
                "private_key_id": "test",
                "private_key": pem,
                "client_email": "tempo@tempo-project.iam.gserviceaccount.com",
                "client_id": "1",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        ),
        encoding="utf-8",
    )
    llm = build_llm_client(
        settings(
            llm_provider="vertex", vertex_project_id="tempo-project", vertex_credentials_file=key
        ),
        timeout=5,
    )
    assert llm is not None and llm.provider == "vertex"
    assert not llm.server_fallbacks and llm.extra() == {}
    interp, calls = interpreter_with(
        lambda _: SimpleNamespace(stop_reason="end_turn", parsed_output=None),
        server_fallbacks=False,
    )
    interp.extract("x", TODAY, [])
    assert "fallbacks" not in calls[0] and "betas" not in calls[0]


def test_request_shape_and_parsed_output() -> None:
    expected = Extraction(items=[], replan=True)
    interp, calls = interpreter_with(
        lambda _: SimpleNamespace(stop_reason="end_turn", parsed_output=expected)
    )
    assert interp.extract("replanifie tout", TODAY, ["home", "club"]) is expected
    call = calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_format"] is Extraction
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["output_config"] == {"effort": "low"}
    content = call["messages"][0]["content"]
    assert "2026-12-07" in content and "home, club" in content


def test_refusal_returns_none() -> None:
    interp, _ = interpreter_with(
        lambda _: SimpleNamespace(stop_reason="refusal", parsed_output=None)
    )
    assert interp.extract("x", TODAY, []) is None


def _error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    "error",
    [
        _error(anthropic.AuthenticationError, 401),
        _error(anthropic.RateLimitError, 429),
        _error(anthropic.InternalServerError, 500),
        anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")),
    ],
)
def test_api_failures_degrade_to_none(error: Exception) -> None:
    def fail(_: Any) -> Any:
        raise error

    interp, _ = interpreter_with(fail)
    assert interp.extract("x", TODAY, []) is None


def test_oversized_text_is_not_sent() -> None:
    interp, calls = interpreter_with(lambda _: None)
    assert interp.extract("x" * (MAX_INPUT_CHARS + 1), TODAY, []) is None
    assert calls == []
