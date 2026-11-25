"""Optional LLM access: Gemini on Google Cloud Vertex AI (the only provider).

Principles (ARCHITECTURE.md, CLAUDE.md): the AI only INTERPRETS; deterministic code then
validates every field (dates in range, positive durations, known places...) exactly like a
user request, and the planner decides feasibility. Tempo works fully without it: when Vertex
AI is not configured, ``build_gemini_client`` / ``build_interpreter`` return ``None`` and
callers use the deterministic path.

``build_gemini_client`` is shared with the conversational agent: Google Gen AI SDK, Vertex AI,
service-account credentials (``tempo_gcp_credentials``), billed by the Google Cloud project.
``GeminiInterpreter`` reads free-text coach e-mails with a JSON-schema structured output.
"""

import logging
import time
from datetime import date
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from tempo.config import LlmSettings

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 20_000
VERTEX_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


class ExtractedItem(BaseModel):
    kind: Literal["study_task", "training", "race"]
    title: str = Field(description="Study subject, or training/race title, in the user's words")
    minutes: int = Field(description="Study effort, or session duration, in minutes")
    day: str = Field(description="ISO date YYYY-MM-DD: study target date or session day")
    start_time: str | None = Field(
        default=None, description="HH:MM local time if stated (mandatory for a race)"
    )
    hard_deadline: bool = Field(
        default=False, description="True only if the text explicitly says the date is strict"
    )
    place_id: str | None = Field(default=None, description="One of the known place ids, or null")
    notes: str = Field(default="", description="Prescribed content kept verbatim (trainings)")


class Extraction(BaseModel):
    items: list[ExtractedItem]
    replan: bool = False
    not_understood: list[str] = Field(
        default_factory=list, description="Parts of the text that are not one of the items"
    )


class Interpreter(Protocol):
    def extract(self, text: str, today: date, place_ids: list[str]) -> Extraction | None:
        """Structured reading of ``text``; None when the model could not help (error/refusal)."""
        ...


SYSTEM = (
    "You convert French messages from a student (or their running coach) into structured "
    "planning items for the Tempo planner. Only extract what is explicitly stated; never "
    "invent dates, durations or places. Study tasks have a SOFT target date unless the text "
    "explicitly says the deadline is strict. Trainings keep their prescribed content verbatim "
    "in notes. Races must have an explicit start time, otherwise put the line in "
    "not_understood. Resolve relative dates (demain, vendredi...) against the given today "
    "date; a weekday name means its next occurrence after today. Use only the given place ids."
)


def build_gemini_client(settings: LlmSettings) -> Any | None:
    """Google Gen AI client on Vertex AI, or None when the project id or the service-account
    key file is missing (logged: Tempo then works without LLM, nothing is guessed)."""
    path = settings.vertex_credentials_file
    if settings.vertex_project_id is None or not path.is_file():
        logger.warning(
            "LLM disabled: Vertex AI needs TEMPO_VERTEX_PROJECT_ID and the service-account "
            "key file (secret tempo_gcp_credentials)"
        )
        return None
    from google import genai
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        str(path), scopes=list(VERTEX_SCOPES)
    )
    return genai.Client(
        vertexai=True,
        project=settings.vertex_project_id,
        location=settings.vertex_region,
        credentials=credentials,
    )


def log_usage(purpose: str, model: str, response: Any, started: float) -> None:
    """One line per model call: tokens (prompt, of which served from Gemini's implicit cache,
    output, thinking) and latency. Never the prompt, the answer or tool arguments."""
    usage = getattr(response, "usage_metadata", None)

    def count(name: str) -> int:
        return int(getattr(usage, name, None) or 0)

    logger.info(
        "llm usage purpose=%s model=%s latency_ms=%d prompt=%d cached=%d output=%d thoughts=%d",
        purpose,
        model,
        round((time.monotonic() - started) * 1000),
        count("prompt_token_count"),
        count("cached_content_token_count"),
        count("candidates_token_count"),
        count("thoughts_token_count"),
    )


def _prompt(text: str, today: date, place_ids: list[str]) -> str | None:
    """The interpretation request (None: text too long, not sent)."""
    if len(text) > MAX_INPUT_CHARS:
        logger.warning("text too long for interpretation (%d chars): not sent", len(text))
        return None
    return (
        f"Today is {today.isoformat()} ({today.strftime('%A')}). "
        f"Known place ids: {', '.join(place_ids) or 'none'}.\n\nMessage:\n{text}"
    )


class GeminiInterpreter:
    """Structured reading of free text with Gemini's JSON-schema output."""

    def __init__(self, client: Any, *, model: str, timeout: float = 60.0) -> None:
        self._client = client
        self._model = model
        self._timeout = timeout

    def extract(self, text: str, today: date, place_ids: list[str]) -> Extraction | None:
        from google.genai import errors, types

        prompt = _prompt(text, today, place_ids)
        if prompt is None:
            return None
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_json_schema=Extraction.model_json_schema(),
            http_options=types.HttpOptions(timeout=int(self._timeout * 1000)),
        )
        started = time.monotonic()
        try:
            response = self._client.models.generate_content(
                model=self._model, contents=prompt, config=config
            )
        except errors.APIError as exc:
            logger.warning("LLM: API error %s", exc.code)
            return None
        except (httpx.HTTPError, OSError):
            logger.warning("LLM: connection error")
            return None
        log_usage("interpret", self._model, response, started)
        if not response.text:
            logger.warning("LLM: no answer (declined or empty)")
            return None
        try:
            return Extraction.model_validate_json(response.text)
        except ValueError:
            logger.warning("LLM: answer does not match the schema: ignored")
            return None


def build_interpreter(settings: LlmSettings) -> Interpreter | None:
    client = build_gemini_client(settings)
    return None if client is None else GeminiInterpreter(client, model=settings.llm_model)
