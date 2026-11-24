"""Optional LLM interpretation (Claude) for text the deterministic parser does not understand.

Principles (ARCHITECTURE.md, CLAUDE.md): the AI only INTERPRETS; deterministic code then
validates every field (dates in range, positive durations, known places...) exactly like a
user request, and the planner decides feasibility. Tempo works fully without it: when no API
key is configured, ``build_interpreter`` returns ``None`` and callers report "not understood".

Uses the official Anthropic SDK with structured outputs (``messages.parse`` + a Pydantic
schema) and the server-side refusal fallback (``fallbacks="default"``).
"""

import logging
from datetime import date
from typing import Literal, Protocol

import anthropic
from pydantic import BaseModel, Field, SecretStr

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
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


class ClaudeInterpreter:
    def __init__(self, api_key: SecretStr, *, model: str = MODEL, timeout: float = 60.0) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key.get_secret_value(), timeout=timeout, max_retries=2
        )
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
            response = self._client.beta.messages.parse(
                model=self._model,
                max_tokens=16000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_format=Extraction,
                output_config={"effort": "low"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
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
        return response.parsed_output


def build_interpreter(api_key: SecretStr | None) -> Interpreter | None:
    return None if api_key is None else ClaudeInterpreter(api_key)
