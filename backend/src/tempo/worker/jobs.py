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

import ipaddress
import logging
import socket
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx2
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from tempo.ai.llm import Interpreter
from tempo.db.ics_repository import apply_ics_sync, get_or_create_source
from tempo.db.models import (
    ActivityRow,
    ConversationTurnRow,
    JobRunRow,
    NotificationRow,
    PhonePositionRow,
    ProposalRow,
    ScheduleVersion,
    SettingRow,
    StudyBlockRow,
)
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
from tempo.transport.routing import Journey, RoutingProvider
from tempo.transport.stib import StibRealtime, StibUnavailableError, leg_advice

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


MAX_REDIRECTS = 5
REDIRECTS = frozenset({301, 302, 303, 307, 308})


def is_public_host(host: str) -> bool:
    """Every address the host resolves to is public (no redirect into the internal network)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)


def fetch_ics(
    client: httpx2.Client,
    url: str,
    *,
    allow_http: bool = False,
    host_check: Callable[[str], bool] = is_public_host,
) -> bytes:
    """Download the feed. Redirects are followed manually: each hop must stay https and point to
    a public host. Errors never include the URL (it often embeds a personal token)."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
            raise JobError("iCal URL must use https")
        if not parts.hostname or not host_check(parts.hostname):
            raise JobError("iCal host is not a public address")
        try:
            with client.stream("GET", url, follow_redirects=False) as response:
                if response.status_code in REDIRECTS and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise JobError(f"iCal download failed: HTTP {response.status_code}")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_ICS_BYTES:
                        raise JobError("iCal feed larger than 10 MB: refused")
                    chunks.append(chunk)
                return b"".join(chunks)
        except JobError:
            raise
        except Exception as exc:  # any client error: report its type only, never the URL
            raise JobError(f"iCal download failed: {type(exc).__name__}") from None
    raise JobError("iCal download failed: too many redirects")


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
                f"{exc}. Donnez à Tempo les coordonnées de votre domicile dans la conversation, "
                "ou reprenez la zone « Maison » dans les options de l'intégration.",
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


STIB_LOOKAHEAD = timedelta(minutes=60)


def _stib_notes(stib: StibRealtime | None, journey: Journey | None, now: datetime) -> list[str]:
    """Official STIB realtime notes for STIB legs boarding within the next hour (on demand,
    cached and quota-capped by ``StibRealtime``)."""
    if stib is None or journey is None:
        return []
    legs = [
        leg
        for leg in journey.legs
        if leg.agency == "STIB" and leg.start.astimezone(UTC) - now <= STIB_LOOKAHEAD
    ]
    if not legs:
        return []
    try:
        snapshot = stib.snapshot()
    except StibUnavailableError as exc:
        return [f"STIB temps réel indisponible ({exc}) : horaire théorique uniquement"]
    notes: list[str] = []
    for leg in legs:
        notes += leg_advice(snapshot, leg.origin_stop_id, leg.line, leg.scheduled_start)
    return list(dict.fromkeys(notes))


def departure_job(
    sessions: Sessions,
    routing: RoutingProvider,
    now: datetime,
    stib: StibRealtime | None = None,
) -> str | None:
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
            "warnings": [*advice.warnings, *_stib_notes(stib, advice.journey, now)],
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
        _reminder(session, advice.status, advice.leave_at, trip, now)
        session.commit()
        return advice.status.value


DEFAULT_REMINDERS = (60, 15)
REMINDER_STATUSES = frozenset(
    {DepartureStatus.ON_TIME, DepartureStatus.LEAVE_NOW, DepartureStatus.LEAVE_EARLIER}
)


def _reminder(
    session: Session,
    status: DepartureStatus,
    leave_at: datetime | None,
    trip: _Trip,
    now: datetime,
) -> None:
    """Configurable departure reminders (setting ``reminder_minutes``, default 60 and 15): the
    smallest threshold already reached fires once per trip."""
    if leave_at is None or status not in REMINDER_STATUSES:
        return
    left = (leave_at - now).total_seconds() / 60
    thresholds = sorted(get_setting(session, "reminder_minutes", list(DEFAULT_REMINDERS)) or [])
    reached = [m for m in thresholds if 0 <= left <= m]
    if not reached:
        return
    minutes = reached[0]
    local = leave_at.strftime("%H:%M")
    notify(
        session,
        "departure_reminder",
        f"Départ dans {max(0, round(left))} min pour {trip.title}",
        f"Départ conseillé à {local} (rappel {minutes} min).",
        f"reminder:{trip.end.isoformat()}:{minutes}",
    )


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


# --- retention ---

NOTIFICATION_RETENTION = timedelta(days=90)
PROPOSAL_RETENTION = timedelta(days=180)
VERSION_RETENTION = timedelta(days=30)
ACTIVITY_RETENTION = timedelta(days=180)


def _deleted(session: Session, statement: Any) -> int:
    result = session.execute(statement)
    return int(getattr(result, "rowcount", 0) or 0)


def retention_job(sessions: Sessions, now: datetime) -> dict[str, int]:
    """Purge personal data that is no longer useful. ``audit_entry`` is append-only for the
    runtime role and is pruned only by the owner role (README.md)."""
    with sessions() as session:
        old_versions = select(ScheduleVersion.id).where(
            ScheduleVersion.state.in_(("superseded", "rejected")),
            ScheduleVersion.created_at < now - VERSION_RETENTION,
        )
        counts = {
            "study_blocks": _deleted(
                session,
                delete(StudyBlockRow).where(StudyBlockRow.schedule_version_id.in_(old_versions)),
            ),
            "schedule_versions": _deleted(
                session, delete(ScheduleVersion).where(ScheduleVersion.id.in_(old_versions))
            ),
            "notifications": _deleted(
                session,
                delete(NotificationRow).where(
                    NotificationRow.acknowledged_at < now - NOTIFICATION_RETENTION
                ),
            ),
            "proposals": _deleted(
                session,
                delete(ProposalRow).where(
                    ProposalRow.status != "pending",
                    ProposalRow.decided_at < now - PROPOSAL_RETENTION,
                ),
            ),
            "activities": _deleted(
                session,
                delete(ActivityRow).where(ActivityRow.latest_end_at < now - ACTIVITY_RETENTION),
            ),
        }
        session.commit()
        return counts


# --- user reminders, recurring events, conversation retention ---

REMINDER_GRACE = timedelta(hours=6)
"""A reminder missed during an outage still fires after a restart, up to this late."""


def _reminder_due(session: Session, row: Any, now: datetime, tz: ZoneInfo) -> list[datetime]:
    """Due instants of ``row`` in ``(now - REMINDER_GRACE, now]`` (several for a recurrence)."""
    if row.target_type == "time":
        if row.remind_at is None:
            return []
        if row.rrule is None:
            return [row.remind_at] if row.remind_at <= now else []
        from dateutil.rrule import rrulestr

        local = row.remind_at.astimezone(tz).replace(tzinfo=None)
        rule = rrulestr(row.rrule, dtstart=local)
        lower = (now - REMINDER_GRACE).astimezone(tz).replace(tzinfo=None)
        upper = now.astimezone(tz).replace(tzinfo=None)
        return [naive.replace(tzinfo=tz) for naive in rule.between(lower, upper, inc=True)]
    start = _reminder_target_start(session, row)
    if start is None:
        return []
    due = start - timedelta(minutes=row.offset_minutes)
    return [due] if due <= now < start + timedelta(minutes=1) else []


def _reminder_target_start(session: Session, row: Any) -> datetime | None:
    """Current start of the target; cancels the reminder when its target is gone."""
    import uuid

    from tempo.db.models import FixedEventRow

    if row.target_type == "activity":
        act = session.get(ActivityRow, uuid.UUID(row.target_id))
        if act is None or act.status in ("cancelled", "done", "missed"):
            row.status = "cancelled"
            return None
        return act.earliest_start_at if act.scheduling == "fixed" else act.planned_start_at
    event = session.get(FixedEventRow, uuid.UUID(row.target_id))
    if event is None or event.deleted_at is not None or event.status == "cancelled":
        row.status = "cancelled"
        return None
    return event.start_at


def reminders_job(sessions: Sessions, now: datetime, tz: ZoneInfo) -> int:
    """Fire due reminders as notifications. Identity = reminder + due instant, so a restart
    never duplicates one and a moved target gets a fresh reminder at its new time."""
    from tempo.db.models import ReminderRow

    fired = 0
    with sessions() as session:
        for row in session.scalars(select(ReminderRow).where(ReminderRow.status == "active")):
            for due in _reminder_due(session, row, now, tz):
                if now - due > REMINDER_GRACE:
                    continue
                late = now - due > timedelta(minutes=5)
                notify(
                    session,
                    "reminder",
                    "Rappel" + (" (en retard)" if late else ""),
                    row.message,
                    f"user_reminder:{row.id}:{due.astimezone(UTC).isoformat()}",
                )
                fired += 1
            one_shot = row.target_type == "time" and row.rrule is None
            if one_shot and row.remind_at is not None and row.remind_at <= now:
                row.status = "done"
        session.commit()
    return fired


def series_job(sessions: Sessions, now: datetime) -> int:
    """Keep recurring personal events materialised on the rolling horizon."""
    from tempo.assistant.catalog import expand_all_series

    with sessions() as session:
        created = expand_all_series(session, now)
        if created:
            set_setting(session, "replan_requested", now.isoformat())
        session.commit()
        return created


CONVERSATION_RETENTION = timedelta(days=7)
IDEMPOTENCY_RETENTION = timedelta(days=2)
"""An ``Idempotency-Key`` replays its stored response for this long, then it is purged."""


def conversation_retention_job(sessions: Sessions, now: datetime) -> int:
    """Delete conversation turns older than 7 days and expired idempotency records."""
    with sessions() as session:
        turns = _deleted(
            session,
            delete(ConversationTurnRow).where(
                ConversationTurnRow.created_at < now - CONVERSATION_RETENTION
            ),
        )
        keys = _deleted(
            session,
            delete(SettingRow).where(
                SettingRow.key.like("idem:%"),
                SettingRow.value["value"]["at"].astext
                < (now - IDEMPOTENCY_RETENTION).astimezone(UTC).isoformat(),
            ),
        )
        session.commit()
        return turns + keys
