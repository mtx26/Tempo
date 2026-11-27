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
from tempo.const import WORKER_HEARTBEAT_MAX_AGE
from tempo.db.store import job_statuses, replan_request
from tempo.transport.feeds import FEEDS, Operator
from tempo.transport.motis import HEALTH_PATH, MotisClient

_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


def _probe(client: httpx2.Client, url: str | None, path: str) -> dict[str, Any]:
    if url is None:
        return {"status": "unconfigured", "reachable": False, "operational": False}
    started = monotonic()
    reachable = False
    try:
        response = client.get(url.rstrip("/") + path)
        reachable = True
        details = response.json()
        if not isinstance(details, dict):
            raise ValueError("invalid health payload")
    except (httpx2.HTTPError, ValueError) as exc:
        return {
            "status": "unavailable",
            "reachable": reachable,
            "operational": False,
            "last_error": type(exc).__name__,
            "response_ms": round((monotonic() - started) * 1000, 2),
        }
    report: dict[str, Any] = {
        "reachable": True,
        "response_ms": round((monotonic() - started) * 1000, 2),
    }
    if path == HEALTH_PATH:
        fields = {k: details[k] for k in ("rt", "gbfs") if isinstance(details.get(k), bool)}
        operational = response.is_success and len(fields) == 2
        report.update(
            status="operational" if operational else "unavailable",
            operational=operational,
            readiness=fields,
            last_error=None if operational else "motis_not_ready",
        )
    elif response.is_success:
        report.update(_relay_status(details))
    else:
        report.update(status="unavailable", operational=False, last_error="relay_http_error")
    return report


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC).isoformat() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _relay_status(payload: dict[str, Any]) -> dict[str, Any]:
    raw_feeds = payload.get("feeds")
    feeds: dict[str, Any] = {}
    if isinstance(raw_feeds, dict):
        for item in FEEDS:
            raw = raw_feeds.get(item.name)
            if not item.is_realtime or not isinstance(raw, dict):
                continue
            state = raw.get("state")
            states = ("operational", "degraded", "stale", "unavailable", "invalid")
            counters = raw.get("entity_freshness")
            feeds[item.name] = {
                "state": state if state in states else "unavailable",
                "last_success_at": _timestamp(raw.get("last_success_at")),
                "produced_at": _timestamp(raw.get("produced_at")),
                "last_error": "feed_fetch_or_validation_error" if raw.get("last_error") else None,
                **{
                    k: raw[k]
                    for k in (
                        "age_seconds",
                        "response_ms",
                        "consecutive_errors",
                        "entities",
                        "trip_updates",
                        "cancelled_trips",
                        "alerts",
                    )
                    if type(raw.get(k)) in (int, float)
                },
                "entity_freshness": {
                    k: counters[k]
                    for k in ("fresh", "stale", "missing_timestamp", "future")
                    if isinstance(counters, dict) and isinstance(counters.get(k), int)
                },
            }
    states_found = {f["state"] for f in feeds.values()}
    operational = states_found == {"operational"}
    missing_timestamps = any(
        f["entity_freshness"].get("missing_timestamp", 0) for f in feeds.values()
    )
    static = payload.get("static_only_operators")
    return {
        "status": "operational" if operational else "degraded" if feeds else "unavailable",
        "operational": operational,
        "freshness": "stale"
        if "stale" in states_found
        else "fresh"
        if operational and not missing_timestamps
        else "unknown",
        "feeds": feeds,
        "paused_until": _timestamp(payload.get("paused_until")),
        "static_only_operators": [
            item.value for item in Operator if isinstance(static, list) and item.value in static
        ],
    }


def _database_status(factory: sessionmaker[Session] | None) -> dict[str, Any]:
    if factory is None:
        return {"status": "unconfigured"}
    started = monotonic()
    try:
        with factory() as session:
            session.execute(text("SET LOCAL statement_timeout = '1500ms'"))
            jobs = job_statuses(session)
            replan_pending = replan_request(session) is not None
            stats: dict[str, Any] = {}
            try:
                with session.begin_nested():
                    stats = dict(
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
            except SQLAlchemyError:
                logger.info("diagnostics PostgreSQL optional metrics unavailable")
    except SQLAlchemyError as exc:
        logger.warning("diagnostics database error=%s", type(exc).__name__)
        return {"status": "unavailable"}
    return {
        "status": "ok" if stats else "metrics_unavailable",
        "response_ms": round((monotonic() - started) * 1000, 2),
        **stats,
        "jobs": jobs,
        "replan_pending": replan_pending,
    }


def _worker_status(database: dict[str, Any], now: datetime) -> dict[str, Any]:
    jobs = database.get("jobs", ())
    last = max(
        (datetime.fromisoformat(job["last_finished"]) for job in jobs if job["last_finished"]),
        default=None,
    )
    if last is None:
        state = "missing_heartbeat" if "jobs" in database else "unknown"
    else:
        state = "active" if now - last <= WORKER_HEARTBEAT_MAX_AGE else "stale_heartbeat"
    return {"status": state, "heartbeat_at": None if last is None else last.isoformat()}


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
        database = _database_status(getattr(app.state, "sessions", None))
        return {
            "backend": {"status": "ok", "version": __version__},
            "postgresql": database,
            "worker": _worker_status(database, app.state.clock()),
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
