"""Background jobs (milestone 7). Each job is a plain function over a session factory so it can
be tested against a real PostgreSQL without the scheduling loop.

- ``ics_sync``: download the university feed (URL is a secret, never stored), parse window +
  margin (D017), reconcile, notify near-term changes and request a re-plan when anything changed.
  Protected commitments are never moved here: the re-plan turns conflicts into proposals.
- ``replan``: run the planning pipeline when requested, nightly, or when the plan is stale.
- ``departure_check``: re-check the next outbound trip from the phone's CURRENT position with live
  routing and notify the user when they must leave now / earlier, are at risk or late.
A failing job records its error in ``job_run`` in a separate transaction (security review item:
a failed sync must leave a trace even though its own transaction was rolled back).
"""

import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx2
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from tempo.ai.llm import Interpreter
from tempo.db.ics_repository import apply_ics_sync, get_or_create_source
from tempo.db.models import JobRunRow, PhonePositionRow
from tempo.planning.service import (
    PlanningNotConfiguredError,
    active_version,
    get_setting,
    notify,
    replan,
    set_setting,
)
from tempo.sync.ics import IcsParseError, parse_ics
from tempo.sync.training_mail import MailSource, import_messages
from tempo.transport.departure import DepartureStatus, check_departure
from tempo.transport.location import LocatedEvent, PhonePosition
from tempo.transport.places import Coordinates, Place
from tempo.transport.routing import RoutingProvider

logger = logging.getLogger(__name__)

ICS_PAST = timedelta(days=1)
ICS_FUTURE = timedelta(days=60)
ICS_MARGIN = timedelta(days=7)
"""Parsed beyond the reconciled window so events moving out of it are seen as changed (D017)."""
MAX_ICS_BYTES = 10 * 1024 * 1024
NEAR_TERM_CHANGE = timedelta(days=7)
DEPARTURE_LOOKAHEAD = timedelta(hours=2)
ALERT_STATUSES = frozenset(
    {
        DepartureStatus.LEAVE_NOW,
        DepartureStatus.LEAVE_EARLIER,
        DepartureStatus.AT_RISK,
        DepartureStatus.LATE,
        DepartureStatus.IMPOSSIBLE,
    }
)

Sessions = sessionmaker[Session]


class JobError(RuntimeError):
    pass


def record_run(sessions: Sessions, name: str, started: datetime, error: str | None) -> None:
    """Separate transaction: the job's own transaction may have been rolled back."""
    with sessions() as session:
        values = {
            "name": name,
            "last_started_at": started,
            "last_finished_at": datetime.now(UTC),
            "last_status": "error" if error else "ok",
            "last_error": None if error is None else error[:2000],
        }
        stmt = insert(JobRunRow).values(**values)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["name"], set_={k: stmt.excluded[k] for k in values if k != "name"}
            )
        )
        session.commit()


def run_job(sessions: Sessions, name: str, job: Callable[[], Any]) -> bool:
    started = datetime.now(UTC)
    try:
        job()
    except Exception as exc:
        logger.error("job %s failed: %s", name, exc)
        logger.debug("%s", traceback.format_exc())
        record_run(sessions, name, started, f"{type(exc).__name__}: {exc}")
        return False
    record_run(sessions, name, started, None)
    return True


# --- iCal ---


def fetch_ics(client: httpx2.Client, url: str, *, allow_http: bool = False) -> bytes:
    scheme = urlsplit(url).scheme
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise JobError("iCal URL must use https")
    try:
        with client.stream("GET", url, follow_redirects=True) as response:
            if response.status_code != 200:
                raise JobError(f"iCal download failed: HTTP {response.status_code}")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_ICS_BYTES:
                    raise JobError("iCal feed larger than 10 MB: refused")
                chunks.append(chunk)
    except httpx2.HTTPError as exc:
        # Never include the URL: it is a secret (it often embeds a personal token).
        raise JobError(f"iCal download failed: {type(exc).__name__}") from None
    return b"".join(chunks)


def ics_sync(
    sessions: Sessions,
    data: bytes,
    now: datetime,
    tz: ZoneInfo,
    *,
    source_name: str = "université",
) -> dict[str, int]:
    window = (now - ICS_PAST, now + ICS_FUTURE)
    try:
        imported = parse_ics(data, window[0], window[1] + ICS_MARGIN, default_tz=tz)
    except IcsParseError as exc:
        raise JobError(str(exc)) from None
    with sessions() as session:
        source = get_or_create_source(session, source_name)
        run = apply_ics_sync(session, source, imported, window, now=now)
        counts = {
            "added": run.added,
            "removed": run.removed,
            "changed": run.changed,
            "restored": run.restored,
        }
        changed = sum(counts.values())
        if run.status == "suspect":
            notify(
                session,
                "ics_suspect",
                "Horaire universitaire suspect",
                "Le flux iCal semble vide ou a perdu beaucoup d'événements : aucune suppression "
                "n'a été appliquée. Vérifiez le calendrier de l'université.",
                f"ics_suspect:{now.date()}",
            )
        elif changed:
            near = [
                e
                for e in imported.events
                if now <= e.start.astimezone(UTC) <= now + NEAR_TERM_CHANGE
            ]
            if near or run.removed:
                notify(
                    session,
                    "ics_changed",
                    "Horaire universitaire modifié",
                    f"{run.added} ajout(s), {run.changed} modification(s), {run.removed} "
                    f"suppression(s). Le planning d'étude est recalculé ; les blocs protégés ne "
                    "bougent qu'après votre accord.",
                    f"ics_changed:{run.id}",
                )
            set_setting(session, "replan_requested", now.isoformat())
        for warning in imported.warnings:
            notify(
                session,
                "ics_warning",
                "Horaire : élément à vérifier",
                warning,
                f"icsw:{warning}"[:300],
            )
        session.commit()
        return counts


# --- re-planning ---

NIGHTLY = time(3, 0)
STALE_AFTER = timedelta(hours=6)
RETRY_AFTER = timedelta(minutes=15)
"""Minimum delay between automatic attempts (a rejected plan must not loop every minute)."""


def replan_due(session: Session, now: datetime, tz: ZoneInfo) -> str | None:
    """Why a re-plan is due now, or None."""
    version = active_version(session)
    last = None if version is None else datetime.fromisoformat(version.summary["planned_at"])
    requested = get_setting(session, "replan_requested")
    if requested is not None and (last is None or datetime.fromisoformat(requested) > last):
        return "requested"
    attempt = get_setting(session, "last_replan_attempt")
    if attempt is not None and now - datetime.fromisoformat(attempt) < RETRY_AFTER:
        return None
    if last is None:
        return "first plan"
    nightly = datetime.combine(now.astimezone(tz).date(), NIGHTLY, tzinfo=tz)
    if last < nightly <= now:
        return "nightly"
    if now - last >= STALE_AFTER:
        return "stale"
    return None


def replan_job(
    sessions: Sessions, routing: RoutingProvider, now: datetime, tz: ZoneInfo
) -> str | None:
    with sessions() as session:
        reason = replan_due(session, now, tz)
        if reason is None:
            return None
        set_setting(session, "last_replan_attempt", now.isoformat())
        if reason == "requested":
            set_setting(session, "replan_requested", None)
        session.commit()  # the attempt is recorded even if planning crashes below
        try:
            replan(session, routing, now, tz, reason=reason)
        except PlanningNotConfiguredError as exc:
            notify(
                session,
                "setup",
                "Tempo n'est pas encore configuré",
                f"{exc}. Configurez votre domicile dans Home Assistant (service tempo.set_place "
                "puis réglage du domicile).",
                "setup:home",
            )
        session.commit()
        return reason


# --- departure check ---


@dataclass(frozen=True, slots=True)
class _Trip:
    start: datetime
    end: datetime
    departure: datetime | None
    destination: Place
    title: str


def _next_outbound(session: Session, now: datetime) -> _Trip | None:
    version = active_version(session)
    if version is None:
        return None
    home = get_setting(session, "home_place_id")
    for trip in version.summary.get("travel", []):
        end = datetime.fromisoformat(trip["end"])
        start = datetime.fromisoformat(trip["start"])
        if trip["destination"] == home or end <= now or start - now > DEPARTURE_LOOKAHEAD:
            continue
        departure = trip.get("departure")
        return _Trip(
            start,
            end,
            None if departure is None else datetime.fromisoformat(departure),
            Place(
                trip["destination"],
                trip["destination_name"],
                Coordinates(*_coordinates(session, trip["destination"])),
            ),
            trip["destination_name"],
        )
    return None


def _coordinates(session: Session, place_id: str) -> tuple[float, float]:
    from tempo.db.models import PlaceRow

    row = session.get(PlaceRow, place_id)
    if row is None:
        raise JobError(f"place {place_id} disappeared")
    return row.latitude, row.longitude


def departure_job(sessions: Sessions, routing: RoutingProvider, now: datetime) -> str | None:
    with sessions() as session:
        trip = _next_outbound(session, now)
        if trip is None:
            set_setting(session, "departure_check", None)
            session.commit()
            return None
        tracker = get_setting(session, "tracker_entity_id")
        position_row = (
            session.scalars(
                select(PhonePositionRow).where(PhonePositionRow.entity_id == tracker)
            ).first()
            if tracker
            else None
        )
        position = (
            None
            if position_row is None
            else PhonePosition(
                Coordinates(position_row.latitude, position_row.longitude),
                position_row.accuracy_m,
                position_row.observed_at,
                position_row.entity_id,
            )
        )
        # The outbound block ends at the commitment start (the margin is inside the block).
        buffer = timedelta(minutes=int(get_setting(session, "buffer_minutes", 10)))
        target = LocatedEvent(trip.end, trip.end + timedelta(minutes=1), trip.destination)
        advice = check_departure(
            target,
            position,
            routing,
            now=now,
            buffer=buffer,
            planned_departure=trip.departure,
        )
        state = {
            "status": advice.status.value,
            "leave_at": None if advice.leave_at is None else advice.leave_at.isoformat(),
            "minutes_late": advice.minutes_late,
            "reason": advice.reason,
            "warnings": advice.warnings,
            "checked_at": now.isoformat(),
            "event_title": trip.title,
            "event_start": trip.end.isoformat(),
        }
        set_setting(session, "departure_check", state)
        if advice.status in ALERT_STATUSES:
            notify(
                session,
                "departure",
                _departure_title(advice.status, trip.title),
                _departure_message(state),
                f"departure:{trip.end.isoformat()}:{advice.status.value}",
            )
        session.commit()
        return advice.status.value


def _departure_title(status: DepartureStatus, destination: str) -> str:
    return {
        DepartureStatus.LEAVE_NOW: f"Partez maintenant pour {destination}",
        DepartureStatus.LEAVE_EARLIER: f"Partez plus tôt pour {destination}",
        DepartureStatus.AT_RISK: f"Arrivée juste pour {destination}",
        DepartureStatus.LATE: f"Retard prévu pour {destination}",
        DepartureStatus.IMPOSSIBLE: f"Aucun trajet trouvé vers {destination}",
    }.get(status, destination)


def _departure_message(state: dict[str, Any]) -> str:
    parts = []
    if state["leave_at"]:
        parts.append(f"Départ : {datetime.fromisoformat(state['leave_at']).strftime('%H:%M')}")
    if state["minutes_late"]:
        parts.append(f"retard estimé {state['minutes_late']} min")
    if state["reason"]:
        parts.append(state["reason"])
    parts += state["warnings"]
    return " — ".join(parts) or "Vérifiez votre trajet."


# --- training e-mails ---

MAIL_LOOKBACK = timedelta(days=14)


def training_mail_job(
    sessions: Sessions,
    source: MailSource,
    now: datetime,
    tz: ZoneInfo,
    interpreter: Interpreter | None,
) -> dict[str, int]:
    today = now.astimezone(tz).date()
    messages = source.fetch(today - MAIL_LOOKBACK)
    with sessions() as session:
        stats = import_messages(session, messages, tz=tz, today=today, interpreter=interpreter)
        session.commit()
        return stats
