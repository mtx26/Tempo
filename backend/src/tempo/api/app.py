"""FastAPI application factory for the private Tempo API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tempo import __version__
from tempo.api.auth import FailureLimiter, token_matches
from tempo.api.ha import router as ha_router
from tempo.api.planning import router as planning_router
from tempo.config import Settings

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

    app.include_router(ha_router, dependencies=[Depends(require_token)])
    app.include_router(planning_router, dependencies=[Depends(require_token)])
    return app
