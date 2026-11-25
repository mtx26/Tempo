"""Optional LLM interpretation (Claude) for text the deterministic parser does not understand.

Principles (ARCHITECTURE.md, CLAUDE.md): the AI only INTERPRETS; deterministic code then
validates every field (dates in range, positive durations, known places...) exactly like a
user request, and the planner decides feasibility. Tempo works fully without it: when no API
key is configured, ``build_interpreter`` returns ``None`` and callers report "not understood".

Uses the official Anthropic SDK with structured outputs (``messages.parse`` + a Pydantic
schema). Provider (``build_llm_client``, shared with the conversational agent): the Claude API,
with the server-side refusal fallback (``fallbacks="default"``), or Claude on Google Cloud
Vertex AI, which has no server-side fallback: a refusal there takes the deterministic path.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol

import anthropic
from pydantic import BaseModel, Field

from tempo.config import LlmSettings

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_INPUT_CHARS = 20_000


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


@dataclass(frozen=True, slots=True)
class LlmClient:
    """The configured provider's ``messages`` resource and what that provider supports."""

    messages: Any
    provider: str
    server_fallbacks: bool
    """Claude API only: refusals are re-run server-side (``fallbacks="default"``)."""

    def extra(self) -> dict[str, Any]:
        """Provider-specific request arguments."""
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"} if self.server_fallbacks else {}


VERTEX_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


def build_llm_client(settings: LlmSettings, *, timeout: float) -> LlmClient | None:
    """The configured LLM client, or None when the provider is not fully configured (Tempo
    then works without LLM; the missing piece is logged, never guessed)."""
    if settings.llm_provider == "vertex":
        path = settings.vertex_credentials_file
        if settings.vertex_project_id is None or not path.is_file():
            logger.warning(
                "LLM disabled: Vertex AI needs TEMPO_VERTEX_PROJECT_ID and the service-account "
                "key file (secret tempo_gcp_credentials)"
            )
            return None
        from google.oauth2 import service_account  # vertex extra of the SDK

        credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            str(path), scopes=list(VERTEX_SCOPES)
        )
        vertex = anthropic.AnthropicVertex(
            project_id=settings.vertex_project_id,
            region=settings.vertex_region,
            credentials=credentials,
            timeout=timeout,
            max_retries=1,
        )
        return LlmClient(vertex.messages, "vertex", server_fallbacks=False)
    if settings.anthropic_api_key is None:
        return None
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), timeout=timeout, max_retries=1
    )
    return LlmClient(client.beta.messages, "anthropic", server_fallbacks=True)


class ClaudeInterpreter:
    def __init__(self, llm: LlmClient, *, model: str) -> None:
        self._llm = llm
        self._model = model

    def extract(self, text: str, today: date, place_ids: list[str]) -> Extraction | None:
        if len(text) > MAX_INPUT_CHARS:
            logger.warning("text too long for interpretation (%d chars): not sent", len(text))
            return None
        prompt = (
            f"Today is {today.isoformat()} ({today.strftime('%A')}). "
            f"Known place ids: {', '.join(place_ids) or 'none'}.\n\nMessage:\n{text}"
        )
        try:
            response = self._llm.messages.parse(
                model=self._model,
                max_tokens=16000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_format=Extraction,
                output_config={"effort": "low"},
                **self._llm.extra(),
            )
        except anthropic.AuthenticationError:
            logger.error("LLM: invalid API key")
            return None
        except anthropic.RateLimitError:
            logger.warning("LLM: rate limited")
            return None
        except anthropic.APIStatusError as exc:
            logger.warning("LLM: API error %s", exc.status_code)
            return None
        except anthropic.APIConnectionError:
            logger.warning("LLM: connection error")
            return None
        if response.stop_reason == "refusal":
            logger.warning("LLM: request declined")
            return None
        parsed: Extraction | None = response.parsed_output
        return parsed


def build_interpreter(settings: LlmSettings) -> Interpreter | None:
    llm = build_llm_client(settings, timeout=60.0)
    return None if llm is None else ClaudeInterpreter(llm, model=settings.llm_model)
