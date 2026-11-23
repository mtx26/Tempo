"""STIB-MIVB realtime (milestone 13): official waiting times and traveller information.

Facts verified on 2026-11-23 (docs/TRANSPORT.md "STIB realtime"):
- STIB publishes NO GTFS-RT; the Belgian Mobility portal serves JSON snapshots anonymously at
  ``<BASE>/rt/WaitingTimes`` and ``<BASE>/rt/TravellersInformation`` (CC BY 4.0). Nested fields
  (``passingtimes``, ``content``, ``lines``, ``points``) are JSON strings to decode again.
- ``pointid`` equals the STIB GTFS ``stop_id`` without its letter suffix (940/940 matched on a
  real snapshot), so MOTIS stop ids ``stib_<stop_id>`` map to points.
- Only the next passages per (stop, line) and free-text messages exist: no trip ids, no
  delays. Tempo therefore uses them only for near-term advice ("next passage at 08:12",
  disruption text) and never rewrites future plans with them.

Quota: these snapshots share the portal quota (anonymous 100/day). Requests happen only on
demand (a departure check with a STIB leg soon), are cached (``cache_for``) and capped per day.
"""

import json
import logging
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
from pydantic import SecretStr

from tempo.transport.feeds import KEY_HEADER

logger = logging.getLogger(__name__)

BASE_URL = "https://api-management-discovery-production.azure-api.net/api/datasets/stibmivb"
WAITING_TIMES = "/rt/WaitingTimes"
TRAVELLERS_INFORMATION = "/rt/TravellersInformation"
ANONYMOUS_DAILY_CAP = 20
STANDARD_DAILY_CAP = 2000


def point_id(stop_id: str | None) -> str | None:
    """``stib_0470F`` / ``0470F`` / ``0470`` -> ``0470``; None for non-STIB stops."""
    if not stop_id:
        return None
    raw = stop_id.removeprefix("stib_")
    if ":" in raw or "_" in raw:
        return None
    match = re.fullmatch(r"(\d{3,5})[A-Z]?", raw)
    return match[1] if match else None


@dataclass(frozen=True, slots=True)
class Passage:
    line: str
    destination: str
    expected: datetime
    message: str | None = None


@dataclass(frozen=True, slots=True)
class StibMessage:
    text: str
    lines: frozenset[str]
    points: frozenset[str]
    priority: int


@dataclass(slots=True)
class StibSnapshot:
    passages: dict[tuple[str, str], list[Passage]] = field(default_factory=dict)
    messages: list[StibMessage] = field(default_factory=list)


class StibUnavailableError(RuntimeError):
    pass


def _json_field(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _lang(texts: Any, language: str = "fr") -> str:
    if isinstance(texts, dict):
        return str(texts.get(language) or texts.get("en") or next(iter(texts.values()), ""))
    return str(texts or "")


def parse_waiting_times(body: dict[str, Any]) -> dict[tuple[str, str], list[Passage]]:
    passages: dict[tuple[str, str], list[Passage]] = {}
    for item in body.get("results", []):
        point = point_id(str(item.get("pointid", "")))
        if point is None:
            continue
        for raw in _json_field(item.get("passingtimes")) or []:
            try:
                expected = datetime.fromisoformat(raw["expectedArrivalTime"])
            except (KeyError, TypeError, ValueError):
                continue  # malformed entry: skipped, never guessed
            if expected.tzinfo is None:
                continue
            line = str(raw.get("lineId") or item.get("lineid") or "")
            message = raw.get("message")
            passages.setdefault((point, line), []).append(
                Passage(
                    line,
                    _lang(raw.get("destination")),
                    expected,
                    _lang(message) if message else None,
                )
            )
    for values in passages.values():
        values.sort(key=lambda p: p.expected.astimezone(UTC))
    return passages


def parse_travellers_information(body: dict[str, Any]) -> list[StibMessage]:
    messages: list[StibMessage] = []
    for item in body.get("results", []):
        content = _json_field(item.get("content")) or []
        texts = [_lang(t) for block in content for t in block.get("text", [])]
        if not texts:
            continue
        lines = frozenset(str(x.get("id")) for x in _json_field(item.get("lines")) or [])
        points = frozenset(
            p for x in _json_field(item.get("points")) or [] if (p := point_id(str(x.get("id"))))
        )
        try:
            priority = int(item.get("priority") or 0)
        except ValueError:
            priority = 0
        messages.append(StibMessage(" ".join(texts), lines, points, priority))
    return messages


class StibRealtime:
    def __init__(
        self,
        *,
        key: SecretStr | None = None,
        http_client: httpx2.Client | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        cache_for: timedelta = timedelta(minutes=15),
    ) -> None:
        self._http = http_client or httpx2.Client(timeout=20.0)
        self._headers = {"User-Agent": "tempo-stib"}
        if key is not None:
            self._headers[KEY_HEADER] = key.get_secret_value()
        self._cap = STANDARD_DAILY_CAP if key is not None else ANONYMOUS_DAILY_CAP
        self._clock = clock
        self._cache_for = cache_for
        self._sent: deque[datetime] = deque()
        self._snapshot: StibSnapshot | None = None
        self._fetched_at: datetime | None = None

    def _get(self, path: str, now: datetime) -> dict[str, Any]:
        while self._sent and now - self._sent[0] >= timedelta(days=1):
            self._sent.popleft()
        if len(self._sent) >= self._cap:
            raise StibUnavailableError("STIB request budget exhausted (rolling 24 h)")
        self._sent.append(now)
        try:
            response = self._http.get(BASE_URL + path, headers=self._headers)
        except httpx2.HTTPError as exc:
            raise StibUnavailableError(f"STIB request failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise StibUnavailableError(f"STIB returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise StibUnavailableError("STIB returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise StibUnavailableError("unexpected STIB payload")
        return body

    def snapshot(self) -> StibSnapshot:
        now = self._clock()
        fresh = self._fetched_at is not None and now - self._fetched_at < self._cache_for
        if self._snapshot is not None and fresh:
            return self._snapshot
        snapshot = StibSnapshot(
            parse_waiting_times(self._get(WAITING_TIMES, now)),
            parse_travellers_information(self._get(TRAVELLERS_INFORMATION, now)),
        )
        self._snapshot, self._fetched_at = snapshot, now
        return snapshot

    @property
    def requests_last_24h(self) -> int:
        return len(self._sent)


def leg_advice(
    snapshot: StibSnapshot, stop_id: str | None, line: str | None, scheduled: datetime
) -> list[str]:
    """Near-term realtime notes for one STIB leg (empty when nothing is published)."""
    point = point_id(stop_id)
    notes: list[str] = []
    if point is None or not line:
        return notes
    upcoming = [
        p
        for p in snapshot.passages.get((point, line), [])
        if p.expected.astimezone(UTC) >= scheduled.astimezone(UTC) - timedelta(minutes=10)
    ]
    if upcoming:
        nxt = upcoming[0]
        local = nxt.expected.astimezone(scheduled.tzinfo)
        notes.append(
            f"STIB temps réel : ligne {line} vers {nxt.destination}, passage prévu à "
            f"{local.strftime('%H:%M')}"
        )
        if nxt.message:
            notes.append(f"STIB ligne {line} : {nxt.message}")
    for message in snapshot.messages:
        if line in message.lines and (not message.points or point in message.points):
            notes.append(f"STIB info ligne {line} : {message.text}")
    return notes
