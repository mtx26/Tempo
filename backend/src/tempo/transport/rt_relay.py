"""Internal GTFS-RT relay between the Belgian Mobility portal and MOTIS.

Why a relay (ARCHITECTURE.md "Belgian feeds"):
- one place enforces the portal quota (budget checked at start, rolling 24 h request log that
  survives restarts when ``state_file`` is set), whatever polling interval MOTIS is configured
  with. Run exactly ONE replica: the request log is per process. The quota is per account, so
  an HTTP ``429`` pauses EVERY feed (``Retry-After`` in seconds or as an HTTP date, never
  shortened); copies that are still fresh keep being served, then MOTIS routes on the static
  timetable;
- the ``bmc-partner-key`` is mandatory (Docker secret ``tempo_bmc_partner_key``: the relay
  refuses to start without it) and is never written to the MOTIS configuration stored in the
  routing data volume; a rejected key (HTTP 401/403) is reported as such in the log and in
  ``/health``;
- feeds are validated (decodable, fresh) and TEC is repaired (``gtfsrt.prepare``) before MOTIS
  sees them. A stale or broken feed is answered with HTTP 503 — never served as live data. MOTIS
  (``incremental_rt_update: false``) then drops its realtime state for that feed: verified live
  on 2026-11-23 (predicted stop times fell from 2/300 to 0/300 once the feed failed).

MOTIS polls ``GET /feeds/<operator>/<kind>.pbf`` on the internal network (no public port). The
relay only serves public open data, but it spends the user's quota, so it must not be exposed.
"""

import json
import logging
import os
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import monotonic

import httpx2
from fastapi import FastAPI, HTTPException, Response
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tempo import __version__
from tempo.const import DOCKER_SECRETS_DIR
from tempo.transport.feeds import (
    FEEDS,
    KEY_HEADER,
    KEY_SECRET,
    Feed,
    FeedKind,
    MissingKeyError,
    Operator,
    PollingPlan,
    check_budget,
    feed,
)
from tempo.transport.gtfsrt import (
    MAX_FEED_AGE,
    TRIP_UPDATE_MAX_AGE,
    FeedSummary,
    InvalidFeedError,
    StaleFeedError,
    check_fresh,
    prepare,
)

logger = logging.getLogger(__name__)

USER_AGENT = f"tempo-rt-relay/{__version__}"
DEFAULT_BACKOFF = timedelta(minutes=5)
MAX_BACKOFF = timedelta(hours=1)
WINDOW = timedelta(hours=24)
KIND_PATHS = {"trip-updates": FeedKind.TRIP_UPDATES, "alerts": FeedKind.SERVICE_ALERTS}


def _max_age(kind: FeedKind) -> timedelta:
    return TRIP_UPDATE_MAX_AGE if kind is FeedKind.TRIP_UPDATES else MAX_FEED_AGE


class RelaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TEMPO_RELAY_",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
        secrets_dir=DOCKER_SECRETS_DIR if DOCKER_SECRETS_DIR.is_dir() else None,
    )

    bmc_partner_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices(KEY_SECRET, "TEMPO_BMC_PARTNER_KEY")
    )
    """Belgian Mobility key, mandatory (Docker secret ``tempo_bmc_partner_key``)."""
    trip_updates_seconds: int | None = None
    alerts_seconds: int | None = None
    feeds: str | None = None
    """Comma-separated ``<operator>/<trip-updates|alerts>`` (default: all six). Any choice must
    still fit the quota budget, otherwise the relay refuses to start."""
    state_file: Path | None = None
    """JSON request log (rolling 24 h) on a persistent volume, so restarts keep the quota."""

    @field_validator("bmc_partner_key")
    @classmethod
    def _non_empty_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            raw = value.get_secret_value()
            if not raw.strip() or any(char.isspace() for char in raw):
                raise ValueError("bmc_partner_key must be a non-empty token without whitespace")
        return value


def parse_feeds(value: str) -> tuple[Feed, ...]:
    selected: list[Feed] = []
    for token in (t.strip() for t in value.split(",")):
        if not token:
            continue
        operator, _, kind = token.partition("/")
        try:
            selected.append(feed(Operator(operator), KIND_PATHS[kind]))
        except (ValueError, KeyError) as exc:
            raise ValueError(f"unknown realtime feed {token!r}") from exc
    if not selected:
        raise ValueError("no realtime feed selected")
    return tuple(dict.fromkeys(selected))


class RelayUnavailableError(RuntimeError):
    """No fresh, valid copy of the feed can be served right now."""


@dataclass(slots=True)
class _Entry:
    payload: bytes | None = None
    summary: FeedSummary | None = None
    fetched_at: datetime | None = None
    retry_at: datetime | None = None
    failures: int = 0
    last_error: str | None = None
    response_ms: float | None = None
    validation: str = "unchecked"
    rejected_produced_at: datetime | None = None


class FeedRelay:
    def __init__(
        self,
        feeds: Iterable[Feed],
        plan: PollingPlan,
        *,
        key: SecretStr,
        http_client: httpx2.Client | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        state_file: Path | None = None,
    ) -> None:
        self._feeds = {(f.operator, f.kind): f for f in feeds}
        if any(not f.is_realtime for f in self._feeds.values()):
            raise ValueError("the relay serves realtime feeds only")
        self._budget = check_budget(self._feeds.values(), plan)
        self._plan = plan
        self._http = http_client or httpx2.Client(timeout=30.0)
        self._headers = {"User-Agent": USER_AGENT, KEY_HEADER: key.get_secret_value()}
        self._clock = clock
        self._entries = {k: _Entry() for k in self._feeds}
        self._locks = {k: threading.Lock() for k in self._feeds}
        self._counter_lock = threading.Lock()
        self._state_file = state_file
        self._sent: deque[datetime] = deque(self._load_state())
        self._exhausted_logged = False
        self._paused_until: datetime | None = None
        """Account-wide pause after an HTTP 429 (the quota is per key, not per feed)."""

    def _load_state(self) -> list[datetime]:
        """Previous request times; a corrupt log refuses to start (quota safety first)."""
        if self._state_file is None or not self._state_file.exists():
            return []
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            sent = sorted(datetime.fromisoformat(item) for item in raw["requests"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"unreadable relay state file {self._state_file}") from exc
        if any(item.tzinfo is None for item in sent):
            raise ValueError(f"naive timestamps in relay state file {self._state_file}")
        return sent

    def _save_state(self) -> None:
        if self._state_file is None:
            return
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"requests": [item.isoformat() for item in self._sent]}), encoding="utf-8"
        )
        os.replace(tmp, self._state_file)

    @property
    def requests_last_24h(self) -> int:
        now = self._clock()
        with self._counter_lock:
            while self._sent and now - self._sent[0] >= WINDOW:
                self._sent.popleft()
            return len(self._sent)

    @property
    def allowed_per_day(self) -> int:
        return self._budget.allowed_per_day

    def enabled(self, operator: Operator, kind: FeedKind) -> bool:
        return (operator, kind) in self._feeds

    def _take_request(self, now: datetime) -> str | None:
        """Reserve one portal request; otherwise the reason no request may be sent."""
        with self._counter_lock:
            if self._paused_until is not None:
                if now < self._paused_until:
                    return f"portal paused after HTTP 429 until {self._paused_until.isoformat()}"
                self._paused_until = None
            while self._sent and now - self._sent[0] >= WINDOW:
                self._sent.popleft()
            if len(self._sent) >= self._budget.allowed_per_day:
                if not self._exhausted_logged:
                    logger.warning(
                        "portal request budget exhausted (%d in 24 h); serving no new data",
                        len(self._sent),
                    )
                    self._exhausted_logged = True
                return "portal request budget exhausted (rolling 24 h)"
            self._exhausted_logged = False
            self._sent.append(now)
            self._save_state()
            return None

    def _pause(self, until: datetime) -> None:
        with self._counter_lock:
            if self._paused_until is None or until > self._paused_until:
                self._paused_until = until

    def _backoff(
        self, entry: _Entry, now: datetime, reason: str, server_wait: timedelta | None = None
    ) -> None:
        entry.failures += 1
        entry.last_error = reason
        ours = min(DEFAULT_BACKOFF * entry.failures, MAX_BACKOFF)
        # A server-imposed delay is never shortened; ours is never below DEFAULT_BACKOFF.
        entry.retry_at = now + max(ours, server_wait or timedelta(0))
        logger.warning("realtime feed fetch failed: %s", reason)

    def _fetch(self, item: Feed, entry: _Entry, now: datetime) -> None:
        refused = self._take_request(now)
        if refused is not None:
            entry.last_error = refused
            return
        started = monotonic()
        entry.validation = "unchecked"
        entry.rejected_produced_at = None
        try:
            response = self._http.get(item.url, headers=self._headers, follow_redirects=False)
        except httpx2.HTTPError as exc:
            self._backoff(entry, now, f"{item.name}: {type(exc).__name__}")
            return
        finally:
            entry.response_ms = round((monotonic() - started) * 1000, 2)
        if response.status_code == 429:
            wait = retry_after(response.headers.get("Retry-After", ""), now)
            self._backoff(entry, now, f"{item.name}: HTTP 429 (quota)", wait)
            if entry.retry_at is not None:
                self._pause(entry.retry_at)
            return
        if response.status_code in (401, 403):
            reason = "key rejected: check the Belgian Mobility subscription and key"
            self._backoff(entry, now, f"{item.name}: HTTP {response.status_code} ({reason})")
            return
        if response.status_code != 200:
            self._backoff(entry, now, f"{item.name}: HTTP {response.status_code}")
            return
        received_at = self._clock()
        try:
            result = prepare(
                response.content,
                repair=item.needs_repair,
                now=received_at,
                max_age=_max_age(item.kind),
            )
            if entry.summary is not None and result.summary.produced_at < entry.summary.produced_at:
                raise InvalidFeedError("feed timestamp regressed")
        except StaleFeedError as exc:
            entry.validation = "stale"
            entry.rejected_produced_at = exc.produced_at
            self._backoff(entry, now, f"{item.name}: {exc}")
            return
        except InvalidFeedError as exc:
            entry.validation = "invalid"
            self._backoff(entry, now, f"{item.name}: {exc}")
            return
        entry.payload, entry.summary, entry.fetched_at = result.payload, result.summary, received_at
        entry.validation = "valid"
        entry.retry_at, entry.failures, entry.last_error = None, 0, None
        logger.info(
            "fetched %s: %d entities, %d start_time repairs",
            item.name,
            result.summary.entities,
            result.start_times_removed,
        )

    def get(self, operator: Operator, kind: FeedKind) -> bytes:
        key = (operator, kind)
        if key not in self._feeds:
            raise KeyError(f"feed {operator.value}/{kind.value} is not enabled")
        item, entry = self._feeds[key], self._entries[key]
        with self._locks[key]:
            now = self._clock()
            due = entry.fetched_at is None or now - entry.fetched_at >= self._plan.interval(kind)
            waiting = entry.retry_at is not None and now < entry.retry_at
            if due and not waiting:
                self._fetch(item, entry, now)
            if entry.payload is None or entry.summary is None:
                raise RelayUnavailableError(entry.last_error or "feed not fetched yet")
            try:
                check_fresh(entry.summary, self._clock(), _max_age(kind))
            except InvalidFeedError as exc:
                raise RelayUnavailableError(f"{exc}; last error: {entry.last_error}") from exc
            return entry.payload

    def status(self) -> dict[str, object]:
        now = self._clock()
        feeds: dict[str, object] = {}
        for key, entry in self._entries.items():
            feeds[self._feeds[key].name] = self._feed_status(
                entry, now, self._feeds[key], self._plan.interval(key[1])
            )
        paused = self._paused_until
        return {
            "paused_until": None if paused is None or paused <= now else paused.isoformat(),
            "requests_last_24h": self.requests_last_24h,
            "allowed_per_day": self._budget.allowed_per_day,
            "static_only_operators": sorted(
                set(Operator) - {item.operator for item in FEEDS if item.is_realtime}
            ),
            "feeds": feeds,
        }

    @staticmethod
    def _feed_status(
        entry: _Entry, now: datetime, item: Feed, interval: timedelta
    ) -> dict[str, object]:
        summary = entry.summary
        produced_at = entry.rejected_produced_at if summary is None else summary.produced_at
        age = None if produced_at is None else now - produced_at
        state = "unavailable"
        if summary is None:
            if entry.validation in ("stale", "invalid"):
                state = entry.validation
        else:
            try:
                check_fresh(summary, now, _max_age(item.kind))
            except StaleFeedError:
                state = "stale"
            except InvalidFeedError:
                state = "invalid"
            else:
                state = (
                    "degraded"
                    if entry.last_error or (age and age > interval + item.publisher_interval)
                    else "operational"
                )
        return {
            "state": state,
            "validation": entry.validation,
            "last_success_at": None if entry.fetched_at is None else entry.fetched_at.isoformat(),
            "produced_at": None if produced_at is None else produced_at.isoformat(),
            "age_seconds": None if age is None else int(age.total_seconds()),
            "entities": None if summary is None else summary.entities,
            "trip_updates": None if summary is None else summary.trip_updates,
            "cancelled_trips": None if summary is None else summary.cancelled_trips,
            "alerts": None if summary is None else summary.alerts,
            "response_ms": entry.response_ms,
            "consecutive_errors": entry.failures,
            "last_error": entry.last_error,
        }


def retry_after(value: str, now: datetime) -> timedelta | None:
    """``Retry-After`` as delay-seconds or HTTP date (RFC 9110); None when absent/unreadable."""
    value = value.strip()
    if value.isdigit():
        return timedelta(seconds=int(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(when - now, timedelta(0))


def relay_from_settings(settings: RelaySettings) -> FeedRelay:
    if settings.bmc_partner_key is None:
        raise MissingKeyError
    feeds = (
        tuple(f for f in FEEDS if f.is_realtime)
        if settings.feeds is None
        else parse_feeds(settings.feeds)
    )
    default, tu_seconds, alert_seconds = (
        PollingPlan(),
        settings.trip_updates_seconds,
        settings.alerts_seconds,
    )
    plan = PollingPlan(
        trip_updates=default.trip_updates if tu_seconds is None else timedelta(seconds=tu_seconds),
        service_alerts=default.service_alerts
        if alert_seconds is None
        else timedelta(seconds=alert_seconds),
    )
    return FeedRelay(feeds, plan, key=settings.bmc_partner_key, state_file=settings.state_file)


def create_relay_app(relay: FeedRelay) -> FastAPI:
    app = FastAPI(
        title="Tempo GTFS-RT relay",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    def health() -> dict[str, object]:
        return relay.status()

    @app.get("/feeds/{operator}/{kind}.pbf")
    def serve(operator: str, kind: str) -> Response:
        try:
            op, feed_kind = Operator(operator), KIND_PATHS[kind]
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=404, detail="unknown feed") from exc
        if not relay.enabled(op, feed_kind):
            raise HTTPException(status_code=404, detail="feed not enabled")
        try:
            payload = relay.get(op, feed_kind)
        except RelayUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(content=payload, media_type="application/x-protobuf")

    return app


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    from tempo.logs import configure_logging

    configure_logging()
    try:
        relay = relay_from_settings(RelaySettings())
    except MissingKeyError as exc:
        raise SystemExit(str(exc)) from None
    uvicorn.run(
        create_relay_app(relay),
        host="0.0.0.0",  # noqa: S104 - container port, internal overlay network only
        port=8090,
        server_header=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
