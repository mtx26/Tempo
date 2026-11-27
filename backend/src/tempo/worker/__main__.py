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
from pydantic_settings import SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tempo.ai.llm import GeminiInterpreter, Interpreter, build_interpreter, selected_model
from tempo.config import LlmSettings
from tempo.const import DOCKER_SECRETS_DIR, TICK_SECONDS
from tempo.sync.training_mail import ImapSource, MailSource
from tempo.transport.motis import MotisClient
from tempo.transport.routing import RoutingProvider, RoutingUnavailableError
from tempo.worker.jobs import (
    conversation_retention_job,
    departure_job,
    fetch_ics,
    ics_sync,
    reminders_job,
    replan_job,
    retention_due,
    retention_job,
    run_job,
    series_job,
    training_mail_job,
)

logger = logging.getLogger("tempo.worker")
HEARTBEAT = Path("/tmp/tempo-worker.alive")  # noqa: S108 - tmpfs of the read-only container
"""Touched after every round: the container health check fails when it gets old."""


class WorkerSettings(LlmSettings):
    model_config = SettingsConfigDict(
        env_prefix="TEMPO_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=DOCKER_SECRETS_DIR if DOCKER_SECRETS_DIR.is_dir() else None,
    )

    database_url: SecretStr
    ics_url: SecretStr | None = None
    """University iCal feed (Docker secret ``tempo_ics_url``): often embeds a personal token."""
    motis_url: str | None = None
    timezone: str = "Europe/Brussels"
    ics_interval_minutes: int = 30
    training_imap_host: str | None = None
    training_imap_user: str | None = None
    training_imap_password: SecretStr | None = None
    """Docker secret ``tempo_training_imap_password`` (prefer an app-specific password)."""
    training_imap_mailbox: str = "INBOX"
    training_allowed_sender: str | None = None
    """The ONLY sender whose e-mails are read (exact address)."""
    training_interval_minutes: int = 30
    training_require_authentication: bool = True
    """Only accept e-mails whose provider recorded DMARC/DKIM pass (anti-spoofing)."""
    training_authserv_id: str | None = None
    """authserv-id of the user's receiving mail server (first word of its
    ``Authentication-Results`` header, e.g. ``mx.google.com``): only its verdict is trusted."""
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
    mail: MailSource | None = None,
    interpreter: Interpreter | None = None,
) -> None:
    """One scheduling round; each job records its own outcome in ``job_run``."""
    ics_url = settings.ics_url
    if due_ics and ics_url is not None:
        url = ics_url.get_secret_value()
        run_job(sessions, "ics_sync", lambda: ics_sync(sessions, fetch_ics(http, url), now, tz))
    if due_ics:
        run_job(sessions, "series", lambda: series_job(sessions, now))
    run_job(
        sessions, "replan", lambda: replan_job(sessions, routing, now, tz), records_success=True
    )
    run_job(sessions, "reminders", lambda: reminders_job(sessions, now, tz))
    if due_departure:
        run_job(sessions, "departure_check", lambda: departure_job(sessions, routing, now, tz))
    run_job(
        sessions,
        "retention",
        lambda: retention_job(sessions, now),
        due=lambda: retention_due(sessions, now, tz),
    )
    run_job(
        sessions,
        "conversation_retention",
        lambda: conversation_retention_job(sessions, now),
        due=lambda: retention_due(sessions, now, tz, "conversation_retention"),
    )
    if mail is not None and due_ics:
        run_job(
            sessions,
            "training_mail",
            lambda: training_mail_job(sessions, mail, now, tz, interpreter),
        )


def mail_source(settings: WorkerSettings) -> MailSource | None:
    required = (
        settings.training_imap_host,
        settings.training_imap_user,
        settings.training_imap_password,
        settings.training_allowed_sender,
    )
    if any(value is None for value in required):
        logger.warning("training e-mail import disabled (IMAP host/user/password/sender not set)")
        return None
    if settings.training_require_authentication and not settings.training_authserv_id:
        logger.warning(
            "training e-mail import disabled: TEMPO_TRAINING_AUTHSERV_ID (receiving server "
            "that verifies DMARC/DKIM) is required to authenticate the coach"
        )
        return None
    assert settings.training_imap_password is not None  # noqa: S101 - narrowed above
    return ImapSource(
        settings.training_imap_host or "",
        settings.training_imap_user or "",
        settings.training_imap_password.get_secret_value(),
        settings.training_allowed_sender or "",
        mailbox=settings.training_imap_mailbox,
        require_authentication=settings.training_require_authentication,
        trusted_authserv_id=settings.training_authserv_id,
    )


def main() -> None:  # pragma: no cover - process loop, jobs are tested individually
    from tempo.logs import configure_logging

    configure_logging()
    settings = WorkerSettings()
    tz = ZoneInfo(settings.timezone)
    engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    routing: RoutingProvider = (
        MotisClient(settings.motis_url, timeout_seconds=30) if settings.motis_url else NoRouting()  # type: ignore[assignment]
    )
    interpreter = build_interpreter(settings)
    mail = mail_source(settings)
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
            if isinstance(interpreter, GeminiInterpreter):
                with sessions() as session:  # model chosen in Home Assistant, no restart
                    interpreter.model = selected_model(session, settings.llm_model)
            tick(
                sessions,
                http,
                routing,
                settings,
                tz,
                now,
                due_ics,
                due_departure,
                mail,
                interpreter,
            )
            last_ics = now if due_ics else last_ics
            last_departure = now if due_departure else last_departure
            HEARTBEAT.touch()
            stop.wait(TICK_SECONDS)
    engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    main()
