"""Technical LLM setting chosen in Home Assistant: which Gemini model Tempo uses.

``GET /v1/llm``: whether an LLM is configured, the model in use, the deployment default and the
text-generation models visible to the Vertex AI project (listed at most once an hour).
``PUT /v1/llm``: choose one of them (``null`` = back to the default). The model is probed with
a minimal request first, so a listed but inaccessible model is refused with a clear message
instead of breaking the conversation. Not a business operation: never exposed to the LLM.
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from tempo.ai.llm import LLM_MODEL_SETTING, list_models, selected_model
from tempo.api.location import SessionDep
from tempo.db.store import audit, set_setting

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1")
LIST_TTL_SECONDS = 3600
PROBE_TIMEOUT_MS = 20_000


class ModelChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(pattern=r"^[a-z0-9][a-z0-9.-]{2,60}$")


def _models(request: Request, client: Any) -> list[str]:
    cached: tuple[float, list[str]] | None = getattr(request.app.state, "llm_models", None)
    if cached is not None and time.monotonic() - cached[0] < LIST_TTL_SECONDS:
        return cached[1]
    try:
        models = list_models(client)
    except Exception as exc:  # listing is a convenience: never block the setting on it
        logger.warning("LLM model listing failed: %s", type(exc).__name__)
        return []
    request.app.state.llm_models = (time.monotonic(), models)
    return models


def _state(request: Request, session: SessionDep) -> dict[str, Any]:
    client = request.app.state.gemini
    default: str = request.app.state.llm_default
    if client is None:
        return {"enabled": False, "model": None, "default": default, "models": []}
    current = selected_model(session, default)
    models = sorted({*_models(request, client), current, default})
    return {"enabled": True, "model": current, "default": default, "models": models}


@router.get("/llm")
def llm_state(request: Request, session: SessionDep) -> dict[str, Any]:
    return _state(request, session)


@router.put("/llm")
def choose_model(body: ModelChoice, request: Request, session: SessionDep) -> dict[str, Any]:
    from google.genai import types

    client = request.app.state.gemini
    if client is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "aucun LLM configuré sur le serveur")
    default: str = request.app.state.llm_default
    name = body.model or default
    if name != default and name not in _models(request, client):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"modèle inconnu du projet : {name}"
        )
    try:
        client.models.generate_content(
            model=name,
            contents="Réponds : ok",
            config=types.GenerateContentConfig(
                max_output_tokens=8, http_options=types.HttpOptions(timeout=PROBE_TIMEOUT_MS)
            ),
        )
    except Exception as exc:
        code = getattr(exc, "code", None)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"modèle inaccessible pour ce projet ({code or type(exc).__name__}) : {name}",
        ) from exc
    set_setting(session, LLM_MODEL_SETTING, body.model)
    audit(session, "home_assistant", "update", "setting", LLM_MODEL_SETTING, model=name)
    return _state(request, session)
