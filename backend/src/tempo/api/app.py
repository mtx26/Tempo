"""FastAPI application factory for the private Tempo API."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import monotonic
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import httpx2
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import RequestResponseEndpoint

from tempo import __version__
from tempo.ai.llm import build_gemini_client
from tempo.api.assistant import router as assistant_router
from tempo.api.auth import FailureLimiter, token_matches
from tempo.api.calendar_feed import calendar_router
from tempo.api.llm import router as llm_router
from tempo.api.location import router as location_router
from tempo.assistant.agent import build_assistant
from tempo.config import Settings
from tempo.db.store import job_statuses, replan_request
from tempo.transport.motis import HEALTH_PATH, MotisClient

_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


def _probe(client: httpx2.Client, url: str | None, path: str) -> dict[str, Any]:
    if url is None:
        return {"status": "unconfigured"}
    started = monotonic()
    try:
        response = client.get(url.rstrip("/") + path)
        response.raise_for_status()
        details = response.json()
        if not isinstance(details, dict):
            raise ValueError("invalid health payload")
    except (httpx2.HTTPError, ValueError) as exc:
        return {
            "status": "unavailable",
            "error": type(exc).__name__,
            "response_ms": round((monotonic() - started) * 1000, 2),
        }
    return {
        "status": "ok",
        "response_ms": round((monotonic() - started) * 1000, 2),
        "details": details,
    }


def _database_status(factory: sessionmaker[Session] | None) -> dict[str, Any]:
    if factory is None:
        return {"status": "unconfigured"}
    started = monotonic()
    try:
        with factory() as session:
            session.execute(text("SET LOCAL statement_timeout = '1500ms'"))
            stats = (
                session.execute(
                    text("""
                SELECT numbackends AS connections, deadlocks,
                       pg_database_size(datid) AS bytes,
                       (SELECT count(*) FROM pg_locks
                         WHERE NOT granted AND database = datid) AS waiting_locks
                FROM pg_stat_database WHERE datname = current_database()
            """)
                )
                .mappings()
                .one()
            )
            jobs = job_statuses(session)
            replan_pending = replan_request(session) is not None
    except SQLAlchemyError as exc:
        logger.warning("diagnostics database error=%s", type(exc).__name__)
        return {"status": "unavailable"}
    return {
        "status": "ok",
        "response_ms": round((monotonic() - started) * 1000, 2),
        **stats,
        "jobs": jobs,
        "replan_pending": replan_pending,
    }


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

    @app.middleware("http")
    async def log_response(request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = monotonic()
        code = 500
        try:
            response = await call_next(request)
            code = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            logger.log(
                logging.WARNING if code >= 500 else logging.INFO,
                "http method=%s route=%s status=%d elapsed_ms=%.2f",
                request.method,
                getattr(route, "path", "unmatched"),
                code,
                (monotonic() - started) * 1000,
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
    def service_status() -> dict[str, str | None]:
        feed = settings.calendar_token
        return {
            "version": __version__,
            "timezone": settings.timezone,
            # Shown in Home Assistant's options so the phone subscription URL can be copied.
            "calendar_path": None if feed is None else f"/calendar/{feed.get_secret_value()}.ics",
        }

    @app.get("/v1/diagnostics", dependencies=[Depends(require_token)])
    def diagnostics() -> dict[str, Any]:
        with httpx2.Client(timeout=2.0, follow_redirects=False) as client:
            motis = _probe(client, settings.motis_url, HEALTH_PATH)
            relay = _probe(
                client,
                None if settings.rt_relay_url is None else str(settings.rt_relay_url),
                "/health",
            )
        return {
            "backend": {"status": "ok", "version": __version__},
            "postgresql": _database_status(getattr(app.state, "sessions", None)),
            "motis": motis,
            "gtfs_realtime": relay,
            "gemini_configured": app.state.assistant is not None,
        }

    app.include_router(location_router, dependencies=[Depends(require_token)])
    app.include_router(assistant_router, dependencies=[Depends(require_token)])
    app.include_router(llm_router, dependencies=[Depends(require_token)])
    if settings.calendar_token is not None:
        token = settings.calendar_token.get_secret_value().encode()
        app.include_router(calendar_router(token, limiter, ZoneInfo(settings.timezone)))
    return app
