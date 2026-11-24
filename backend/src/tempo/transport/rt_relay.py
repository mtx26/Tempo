"""Internal GTFS-RT relay between the Belgian Mobility portal and MOTIS.

Why a relay (ARCHITECTURE.md "Belgian feeds"):
- one place enforces the portal quota (budget checked at start, rolling 24 h request log that
  survives restarts when ``state_file`` is set, ``429`` back-off), whatever polling interval
  MOTIS is configured with. Run exactly ONE replica: the request log is per process;
- the ``bmc-partner-key`` stays in a Docker secret of this service and is never written to the
  MOTIS configuration stored in the routing data volume;
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
from pathlib import Path

import httpx2
from fastapi import FastAPI, HTTPException, Response
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tempo import __version__
from tempo.transport.feeds import (
    FEEDS,
    KEY_HEADER,
    AccessTier,
    Feed,
    FeedKind,
    Operator,
    PollingPlan,
    check_budget,
    feed,
)
from tempo.transport.gtfsrt import FeedSummary, InvalidFeedError, check_fresh, prepare

logger = logging.getLogger(__name__)

DOCKER_SECRETS_DIR = Path("/run/secrets")
USER_AGENT = f"tempo-rt-relay/{__version__}"
DEFAULT_BACKOFF = timedelta(minutes=5)
MAX_BACKOFF = timedelta(hours=1)
WINDOW = timedelta(hours=24)
ANONYMOUS_PLAN = PollingPlan(trip_updates=timedelta(minutes=30), service_alerts=timedelta(hours=6))
"""Anonymous tier (100 requests/day): SNCB trip updates only, every 30 minutes. With the 10 min
freshness limit, live data is then available only ~10 of every 30 minutes (documented)."""
ANONYMOUS_FEEDS: tuple[Feed, ...] = (feed(Operator.SNCB, FeedKind.TRIP_UPDATES),)
KIND_PATHS = {"trip-updates": FeedKind.TRIP_UPDATES, "alerts": FeedKind.SERVICE_ALERTS}


class RelaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TEMPO_RELAY_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=DOCKER_SECRETS_DIR if DOCKER_SECRETS_DIR.is_dir() else None,
    )

    bmc_partner_key: SecretStr | None = None
    """Belgian Mobility Standard key (Docker secret ``tempo_relay_bmc_partner_key``)."""
    trip_updates_seconds: int | None = None
    alerts_seconds: int | None = None
    feeds: str | None = None
    """Comma-separated ``<operator>/<trip-updates|alerts>`` (default: depends on the tier).
    Any choice must still fit the tier's quota budget, otherwise the relay refuses to start."""
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


class FeedRelay:
    def __init__(
        self,
        feeds: Iterable[Feed],
        plan: PollingPlan,
        tier: AccessTier,
        *,
        key: SecretStr | None = None,
        http_client: httpx2.Client | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        state_file: Path | None = None,
    ) -> None:
        self._feeds = {(f.operator, f.kind): f for f in feeds}
        if any(not f.is_realtime for f in self._feeds.values()):
            raise ValueError("the relay serves realtime feeds only")
        if tier is AccessTier.STANDARD and key is None:
            raise ValueError("the standard tier needs a bmc-partner-key")
        self._budget = check_budget(self._feeds.values(), plan, tier)
        self._plan = plan
        self.tier = tier
        headers = {"User-Agent": USER_AGENT}
        if key is not None:
            headers[KEY_HEADER] = key.get_secret_value()
        self._http = http_client or httpx2.Client(timeout=30.0)
        self._headers = headers
        self._clock = clock
        self._entries = {k: _Entry() for k in self._feeds}
        self._locks = {k: threading.Lock() for k in self._feeds}
        self._counter_lock = threading.Lock()
        self._state_file = state_file
        self._sent: deque[datetime] = deque(self._load_state())
        self._exhausted_logged = False

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
        return len(self._sent)

    @property
    def allowed_per_day(self) -> int:
        return self._budget.allowed_per_day

    def enabled(self, operator: Operator, kind: FeedKind) -> bool:
        return (operator, kind) in self._feeds

    def _take_request(self, now: datetime) -> bool:
        with self._counter_lock:
            while self._sent and now - self._sent[0] >= WINDOW:
                self._sent.popleft()
            if len(self._sent) >= self._budget.allowed_per_day:
                if not self._exhausted_logged:
                    logger.warning(
                        "portal request budget exhausted (%d in 24 h); serving no new data",
                        len(self._sent),
                    )
                    self._exhausted_logged = True
                return False
            self._exhausted_logged = False
            self._sent.append(now)
            self._save_state()
            return True

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
        if not self._take_request(now):
            entry.last_error = "portal request budget exhausted (rolling 24 h)"
            return
        try:
            response = self._http.get(item.url, headers=self._headers)
        except httpx2.HTTPError as exc:
            self._backoff(entry, now, f"{item.name}: {type(exc).__name__}")
            return
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "").strip()
            wait = timedelta(seconds=int(retry)) if retry.isdigit() else None
            self._backoff(entry, now, f"{item.name}: HTTP 429 (quota)", wait)
            return
        if response.status_code != 200:
            self._backoff(entry, now, f"{item.name}: HTTP {response.status_code}")
            return
        try:
            result = prepare(response.content, repair=item.needs_repair, now=now)
        except InvalidFeedError as exc:
            self._backoff(entry, now, f"{item.name}: {exc}")
            return
        entry.payload, entry.summary, entry.fetched_at = result.payload, result.summary, now
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
                check_fresh(entry.summary, now)
            except InvalidFeedError as exc:
                raise RelayUnavailableError(f"{exc}; last error: {entry.last_error}") from exc
            return entry.payload

    def status(self) -> dict[str, object]:
        now = self._clock()
        feeds: dict[str, object] = {}
        for (operator, kind), entry in self._entries.items():
            age = None if entry.summary is None else (now - entry.summary.produced_at)
            feeds[f"{operator.value}/{kind.value}"] = {
                "age_seconds": None if age is None else int(age.total_seconds()),
                "entities": None if entry.summary is None else entry.summary.entities,
                "last_error": entry.last_error,
            }
        return {
            "tier": self.tier.value,
            "requests_last_24h": self.requests_last_24h,
            "allowed_per_day": self._budget.allowed_per_day,
            "feeds": feeds,
        }


def relay_from_settings(settings: RelaySettings) -> FeedRelay:
    if settings.bmc_partner_key is None:
        plan, feeds, tier = ANONYMOUS_PLAN, ANONYMOUS_FEEDS, AccessTier.ANONYMOUS
    else:
        plan, tier = PollingPlan(), AccessTier.STANDARD
        feeds = tuple(f for f in FEEDS if f.is_realtime)
    if settings.feeds is not None:
        feeds = parse_feeds(settings.feeds)
    tu_seconds, alert_seconds = settings.trip_updates_seconds, settings.alerts_seconds
    plan = PollingPlan(
        trip_updates=plan.trip_updates if tu_seconds is None else timedelta(seconds=tu_seconds),
        service_alerts=(
            plan.service_alerts if alert_seconds is None else timedelta(seconds=alert_seconds)
        ),
    )
    return FeedRelay(
        feeds, plan, tier, key=settings.bmc_partner_key, state_file=settings.state_file
    )


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
            raise HTTPException(status_code=404, detail="feed not enabled for this tier")
        try:
            payload = relay.get(op, feed_kind)
        except RelayUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(content=payload, media_type="application/x-protobuf")

    return app


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    relay = relay_from_settings(RelaySettings())
    uvicorn.run(
        create_relay_app(relay),
        host="0.0.0.0",  # noqa: S104 - container port, internal overlay network only
        port=8090,
        server_header=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
