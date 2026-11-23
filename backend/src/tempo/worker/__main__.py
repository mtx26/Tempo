"""Worker process: ``python -m tempo.worker`` (one replica).

Runs without Home Assistant or any LLM. Missing optional configuration (iCal URL, MOTIS URL,
training mailbox) disables the matching job with a logged reason; nothing is invented.
"""

import logging
import signal
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx2
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tempo.transport.motis import MotisClient
from tempo.transport.routing import RoutingProvider, RoutingUnavailableError
from tempo.worker.jobs import departure_job, fetch_ics, ics_sync, replan_job, run_job

logger = logging.getLogger("tempo.worker")
SECRETS_DIR = Path("/run/secrets")
TICK_SECONDS = 30


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TEMPO_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=SECRETS_DIR if SECRETS_DIR.is_dir() else None,
    )

    database_url: SecretStr
    ics_url: SecretStr | None = None
    """University iCal feed (Docker secret ``tempo_ics_url``): often embeds a personal token."""
    motis_url: str | None = None
    timezone: str = "Europe/Brussels"
    ics_interval_minutes: int = 30
    departure_interval_minutes: int = 2


class NoRouting:
    """Used when no MOTIS is configured: every query is an explicit outage, never a guess."""

    def plan(self, *args: object, **kwargs: object) -> list[object]:
        raise RoutingUnavailableError("no routing service configured (TEMPO_MOTIS_URL)")


def tick(
    sessions: sessionmaker[Session],
    http: httpx2.Client,
    routing: RoutingProvider,
    settings: WorkerSettings,
    tz: ZoneInfo,
    now: datetime,
    due_ics: bool,
    due_departure: bool,
) -> None:
    """One scheduling round; each job records its own outcome in ``job_run``."""
    ics_url = settings.ics_url
    if due_ics and ics_url is not None:
        url = ics_url.get_secret_value()
        run_job(sessions, "ics_sync", lambda: ics_sync(sessions, fetch_ics(http, url), now, tz))
    run_job(sessions, "replan", lambda: replan_job(sessions, routing, now, tz))
    if due_departure:
        run_job(sessions, "departure_check", lambda: departure_job(sessions, routing, now))


def main() -> None:  # pragma: no cover - process loop, jobs are tested individually
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = WorkerSettings()
    tz = ZoneInfo(settings.timezone)
    engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    routing: RoutingProvider = (
        MotisClient(settings.motis_url, timeout_seconds=30) if settings.motis_url else NoRouting()  # type: ignore[assignment]
    )
    if settings.ics_url is None:
        logger.warning("TEMPO_ICS_URL not set: university timetable sync disabled")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    last_ics = last_departure = datetime.min.replace(tzinfo=UTC)
    with httpx2.Client(timeout=60.0, headers={"User-Agent": "tempo-worker"}) as http:
        while not stop.is_set():
            now = datetime.now(UTC)
            due_ics = now - last_ics >= timedelta(minutes=settings.ics_interval_minutes)
            due_departure = now - last_departure >= timedelta(
                minutes=settings.departure_interval_minutes
            )
            tick(sessions, http, routing, settings, tz, now, due_ics, due_departure)
            last_ics = now if due_ics else last_ics
            last_departure = now if due_departure else last_departure
            stop.wait(TICK_SECONDS)
    engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    main()
