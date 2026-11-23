"""Claude interpreter: request shape and failure handling (SDK call replaced, no network)."""

from datetime import date
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from tempo.ai.llm import (
    FALLBACK_BETA,
    MAX_INPUT_CHARS,
    MODEL,
    ClaudeInterpreter,
    Extraction,
    build_interpreter,
)

KEY = SecretStr("sk-test")
TODAY = date(2026, 12, 7)


def interpreter_with(behaviour: Any) -> tuple[ClaudeInterpreter, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    interp = ClaudeInterpreter(KEY)

    def parse(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return behaviour(kwargs)

    interp._client = SimpleNamespace(  # type: ignore[assignment]
        beta=SimpleNamespace(messages=SimpleNamespace(parse=parse))
    )
    return interp, calls


def test_no_key_means_no_interpreter() -> None:
    assert build_interpreter(None) is None
    assert isinstance(build_interpreter(KEY), ClaudeInterpreter)


def test_request_shape_and_parsed_output() -> None:
    expected = Extraction(items=[], replan=True)
    interp, calls = interpreter_with(
        lambda _: SimpleNamespace(stop_reason="end_turn", parsed_output=expected)
    )
    assert interp.extract("replanifie tout", TODAY, ["home", "club"]) is expected
    call = calls[0]
    assert call["model"] == MODEL == "claude-opus-5-5"
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
