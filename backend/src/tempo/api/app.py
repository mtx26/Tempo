"""FastAPI application factory for the private Tempo API."""

import hmac
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from tempo import __version__
from tempo.config import Settings

_bearer = HTTPBearer(auto_error=False)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(
        title="Tempo", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
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

    return app
