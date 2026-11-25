"""HTTP client for a private MOTIS instance (``GET /api/v6/plan``).

Built from the MOTIS OpenAPI contract read on 2026-11-23 (ARCHITECTURE.md §3) and verified
against a REAL MOTIS v2.11.4 (Brussels and nationwide data, recorded fixtures, live tests). Any
transport or format problem raises ``RoutingUnavailableError`` — never partial data.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx2

from tempo.transport.places import Coordinates
from tempo.transport.routing import Journey, Leg, RoutingUnavailableError, ServiceAlert

PLAN_PATH = "/api/v6/plan"
DEFAULT_TIMEOUT_SECONDS = 10.0


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp without UTC offset")
    return parsed


UNSET_BEFORE = datetime(1970, 1, 2, tzinfo=UTC)


def _optional_time(value: Any) -> datetime | None:
    """Missing bound, or an epoch-0 placeholder for "no bound", means unbounded."""
    if value is None:
        return None
    parsed = _time(value)
    return None if parsed < UNSET_BEFORE else parsed


def _alert(raw: dict[str, Any]) -> ServiceAlert:
    header = str(raw.get("headerText") or "").strip()
    description = str(raw.get("descriptionText") or "").strip()
    effect = str(raw.get("effect") or "UNKNOWN_EFFECT")
    periods = tuple(
        (_optional_time(p.get("start")), _optional_time(p.get("end")))
        for p in raw.get("impactPeriod") or ()
    )
    url = raw.get("url")
    return ServiceAlert(
        header=header or description[:120] or effect,
        description=description,
        cause=str(raw.get("cause") or "UNKNOWN_CAUSE"),
        effect=effect,
        url=str(url) if url else None,
        impact_periods=periods,
    )


def _leg(raw: dict[str, Any]) -> Leg:
    return Leg(
        mode=str(raw["mode"]),
        origin_name=str(raw["from"]["name"]),
        destination_name=str(raw["to"]["name"]),
        start=_time(raw["startTime"]),
        end=_time(raw["endTime"]),
        scheduled_start=_time(raw["scheduledStartTime"]),
        scheduled_end=_time(raw["scheduledEndTime"]),
        realtime=bool(raw["realTime"]),
        cancelled=bool(raw.get("cancelled", False)),
        line=raw.get("routeShortName"),
        agency=raw.get("agencyName"),
        distance_m=raw.get("distance"),
        alerts=tuple(_alert(alert) for alert in raw.get("alerts") or ()),
    )


def _journey(raw: dict[str, Any]) -> Journey:
    legs = tuple(_leg(leg) for leg in raw["legs"])
    if not legs:
        raise ValueError("itinerary without legs")
    return Journey(
        legs=legs,
        departure=_time(raw["startTime"]),
        arrival=_time(raw["endTime"]),
        transfers=int(raw["transfers"]),
        source="motis",
    )


MAX_ERROR_TEXT = 300


def _error_text(response: httpx2.Response) -> str:
    """MOTIS error message (e.g. query outside the loaded timetable window), truncated."""
    try:
        text = str(response.json().get("error", ""))
    except (ValueError, AttributeError):
        text = response.text
    return " ".join(text.split())[:MAX_ERROR_TEXT] or "no error message"


class MotisClient:
    accepts_max_travel = True
    """``plan`` takes ``max_travel`` (MOTIS ``maxTravelTime``): see ``transport.access``."""

    def __init__(
        self,
        base_url: str,
        *,
        http_client: httpx2.Client | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("MOTIS base URL must be an absolute http(s) URL")
        self._base_url = base_url.rstrip("/")
        self._http = http_client or httpx2.Client(timeout=timeout_seconds)

    def plan(
        self,
        origin: Coordinates,
        destination: Coordinates,
        time: datetime,
        *,
        arrive_by: bool,
        max_travel: timedelta | None = None,
    ) -> list[Journey]:
        if time.tzinfo is None or time.utcoffset() is None:
            raise ValueError("query time must be timezone-aware")
        params = {
            "fromPlace": origin.as_motis(),
            "toPlace": destination.as_motis(),
            "time": time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "arriveBy": "true" if arrive_by else "false",
        }
        if max_travel is not None:  # native MOTIS limit (minutes) on the whole journey
            params["maxTravelTime"] = str(max(1, int(max_travel.total_seconds() // 60)))
        try:
            response = self._http.get(self._base_url + PLAN_PATH, params=params)
        except httpx2.HTTPError as exc:
            raise RoutingUnavailableError(f"MOTIS request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise RoutingUnavailableError(
                f"MOTIS returned HTTP {response.status_code}: {_error_text(response)}"
            )
        try:
            body = response.json()
            raw = [*body.get("direct", []), *body["itineraries"]]
            journeys = [_journey(item) for item in raw]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RoutingUnavailableError("malformed MOTIS response") from exc
        return [j for j in journeys if not any(leg.cancelled for leg in j.legs)]
