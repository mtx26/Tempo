"""Gemini access on Vertex AI: configuration, request shape and failure handling (no network)."""

import json
import logging
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tempo.ai.llm import (
    MAX_INPUT_CHARS,
    Extraction,
    GeminiInterpreter,
    build_gemini_client,
    build_interpreter,
)
from tempo.config import LlmSettings

TODAY = date(2026, 12, 7)
MODEL = "gemini-model-under-test"


def settings(**values: Any) -> LlmSettings:
    return LlmSettings(_env_file=None, **values)


def service_account_file(folder: Path) -> Path:
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
    key = folder / "sa.json"
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
    return key


def interpreter_with(behaviour: Any) -> tuple[GeminiInterpreter, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def generate_content(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return behaviour(kwargs)

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    return GeminiInterpreter(client, model=MODEL), calls


def test_incomplete_settings_mean_no_llm(tmp_path: Path) -> None:
    key = service_account_file(tmp_path)
    assert build_interpreter(settings()) is None
    no_project = settings(vertex_credentials_file=key)
    assert build_gemini_client(no_project) is None
    no_key = settings(
        vertex_project_id="tempo-project",
        llm_model=MODEL,
        vertex_credentials_file=tmp_path / "absent.json",
    )
    assert build_gemini_client(no_key) is None


def test_complete_settings_build_a_vertex_client(tmp_path: Path) -> None:
    complete = settings(
        vertex_project_id="tempo-project",
        llm_model=MODEL,
        vertex_credentials_file=service_account_file(tmp_path),
    )
    client = build_gemini_client(complete)
    assert client is not None and client.vertexai
    assert isinstance(build_interpreter(complete), GeminiInterpreter)


def test_request_shape_and_parsed_output() -> None:
    expected = Extraction(items=[], replan=True)
    interp, calls = interpreter_with(lambda _: SimpleNamespace(text=expected.model_dump_json()))
    assert interp.extract("replanifie tout", TODAY, ["home", "club"]) == expected
    call = calls[0]
    assert call["model"] == MODEL
    config = call["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == Extraction.model_json_schema()
    assert config.http_options.timeout == 60_000
    assert "2026-12-07" in call["contents"] and "home, club" in call["contents"]


def test_declined_or_malformed_answer_returns_none() -> None:
    interp, _ = interpreter_with(lambda _: SimpleNamespace(text=None))
    assert interp.extract("x", TODAY, []) is None
    interp, _ = interpreter_with(lambda _: SimpleNamespace(text='{"not": "the schema"}'))
    assert interp.extract("x", TODAY, []) is None


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("down"),
        httpx.ReadTimeout("slow"),
    ],
)
def test_api_failures_degrade_to_none(error: Exception) -> None:
    def fail(_: Any) -> Any:
        raise error

    interp, _ = interpreter_with(fail)
    assert interp.extract("x", TODAY, []) is None


def test_api_error_degrades_to_none() -> None:
    from google.genai import errors

    def fail(_: Any) -> Any:
        raise errors.ClientError(403, {"error": {"message": "denied", "status": "DENIED"}})

    interp, _ = interpreter_with(fail)
    assert interp.extract("x", TODAY, []) is None


def test_oversized_text_is_not_sent() -> None:
    interp, calls = interpreter_with(lambda _: None)
    assert interp.extract("x" * (MAX_INPUT_CHARS + 1), TODAY, []) is None
    assert calls == []


def test_default_model_is_configurable() -> None:
    assert settings().llm_model == "gemini-3.5-flash-lite"
    assert settings(llm_model="gemini-3.5-flash").llm_model == "gemini-3.5-flash"
    with pytest.raises(ValueError):
        settings(llm_model="Bad Model!")


def test_usage_and_latency_are_logged_without_content(caplog: pytest.LogCaptureFixture) -> None:
    usage = SimpleNamespace(
        prompt_token_count=1200,
        cached_content_token_count=1000,
        candidates_token_count=40,
        thoughts_token_count=None,
    )
    answer = Extraction(items=[], replan=True).model_dump_json()
    interp, _ = interpreter_with(lambda _: SimpleNamespace(text=answer, usage_metadata=usage))
    with caplog.at_level(logging.INFO, logger="tempo.ai.llm"):
        interp.extract("Secret du coach : fractionné jeudi", TODAY, ["home"])
    lines = [r.getMessage() for r in caplog.records if "llm usage" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert "purpose=interpret" in line and f"model={MODEL}" in line
    assert "prompt=1200" in line and "cached=1000" in line and "output=40" in line
    assert "thoughts=0" in line and "latency_ms=" in line
    assert "Secret" not in line and "home" not in line
