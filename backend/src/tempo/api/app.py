"""FastAPI application factory for the private Tempo API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tempo import __version__
from tempo.ai.llm import build_gemini_client
from tempo.api.assistant import router as assistant_router
from tempo.api.auth import FailureLimiter, token_matches
from tempo.api.calendar_feed import calendar_router
from tempo.api.llm import router as llm_router
from tempo.api.location import router as location_router
from tempo.assistant.agent import build_assistant
from tempo.config import Settings
from tempo.transport.motis import MotisClient

_bearer = HTTPBearer(auto_error=False)


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = None
        if settings.database_url is not None:
            engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
            app.state.sessions = sessionmaker(engine, expire_on_commit=False)
        yield
        if engine is not None:
            engine.dispose()

    app = FastAPI(
        lifespan=lifespan,
        title="Tempo",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.timezone = ZoneInfo(settings.timezone)
    app.state.clock = lambda: datetime.now(UTC)
    app.state.gemini = build_gemini_client(settings)
    app.state.llm_default = settings.llm_model
    app.state.assistant = build_assistant(app.state.gemini, settings.llm_model)
    app.state.routing = (
        MotisClient(settings.motis_url, timeout_seconds=30) if settings.motis_url else None
    )

    limiter = FailureLimiter()
    app.state.auth_limiter = limiter

    def require_token(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        client = request.client.host if request.client is not None else "unknown"
        wait = limiter.retry_after(client)
        if wait is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many failed authentication attempts",
                headers={"Retry-After": str(wait)},
            )
        presented = None if credentials is None else credentials.credentials
        if not token_matches(presented, settings.accepted_tokens()):
            limiter.failure(client)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        limiter.success(client)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/status", dependencies=[Depends(require_token)])
    def service_status() -> dict[str, str]:
        return {"version": __version__, "timezone": settings.timezone}

    app.include_router(location_router, dependencies=[Depends(require_token)])
    app.include_router(assistant_router, dependencies=[Depends(require_token)])
    app.include_router(llm_router, dependencies=[Depends(require_token)])
    if settings.calendar_token is not None:
        token = settings.calendar_token.get_secret_value().encode()
        app.include_router(calendar_router(token, limiter))
    return app
