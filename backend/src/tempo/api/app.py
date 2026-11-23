"""FastAPI application factory for the private Tempo API."""

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tempo import __version__
from tempo.api.ha import router as ha_router
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

    def require_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        expected = settings.api_token.get_secret_value().encode()
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), expected
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/status", dependencies=[Depends(require_token)])
    def service_status() -> dict[str, str]:
        return {"version": __version__, "timezone": settings.timezone}

    app.include_router(ha_router, dependencies=[Depends(require_token)])
    return app
