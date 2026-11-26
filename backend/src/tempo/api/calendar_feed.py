"""Read-only iCalendar feed of the published plan, for a phone calendar subscription.

``GET /calendar/<token>.ics`` lists what Home Assistant's calendar shows (``schedule_items``)
except classes (the phone already subscribes to the university calendar): study blocks,
activities, trainings, races and journeys, from 14 days ago to 60 days ahead. Nothing can be
changed through it (the conversation stays the only business interface).
Calendar apps cannot send an Authorization header, so the URL carries a dedicated secret
(``calendar_token``), compared in constant time; a wrong token gets 404 (the feed's existence
is not revealed) and counts as a failed attempt for the shared limiter.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response, status
from icalendar import Calendar, Event

from tempo.api.auth import FailureLimiter, token_matches
from tempo.api.location import SessionDep
from tempo.planning.views import schedule_items

PAST = timedelta(days=14)
FUTURE = timedelta(days=60)
REFRESH = "PT15M"


def calendar_router(token: bytes, limiter: FailureLimiter, tz: ZoneInfo) -> APIRouter:
    router = APIRouter()

    @router.get("/calendar/{presented}.ics", include_in_schema=False)
    def feed(presented: str, request: Request, session: SessionDep) -> Response:
        client = request.client.host if request.client is not None else "unknown"
        wait = limiter.retry_after(client)
        if wait is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "too many failed attempts",
                headers={"Retry-After": str(wait)},
            )
        if not token_matches(presented, (token,)):
            limiter.failure(client)
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
        limiter.success(client)
        clock: Callable[[], datetime] = request.app.state.clock
        now = clock()
        items = [
            item
            for item in schedule_items(session, now - PAST, now + FUTURE)
            if item["type"] != "class"  # already on the phone through the university calendar
        ]
        return Response(
            content=to_ical(items, now, tz),
            media_type="text/calendar; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    return router


def to_ical(items: list[dict[str, Any]], now: datetime, tz: ZoneInfo) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//Tempo//Planning//FR")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("x-wr-calname", "Tempo")
    calendar.add("refresh-interval;value=duration", REFRESH)
    calendar.add("x-published-ttl", REFRESH)
    stamp = now.astimezone(UTC)
    for item in items:
        event = Event()
        event.add("uid", f"{item['id']}@tempo")
        event.add("dtstamp", stamp)
        event.add("summary", _title(item))
        start, end = datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
        if item.get("all_day"):
            first: date = start.date()
            event.add("dtstart", first)
            event.add("dtend", max(end.date(), first + timedelta(days=1)))
        else:
            event.add("dtstart", start.astimezone(UTC))
            event.add("dtend", end.astimezone(UTC))
        if item.get("location"):
            event.add("location", item["location"])
        description = _description(item, tz)
        if description:
            event.add("description", description)
        event.add("categories", [item["type"]])
        if item.get("busy") is False:
            event.add("transp", "TRANSPARENT")
        calendar.add_component(event)
    payload: bytes = calendar.to_ical()
    return payload


def _description(item: dict[str, Any], tz: ZoneInfo) -> str:
    if item["type"] == "travel":
        if item.get("status") == "impossible":
            return "Aucun trajet possible à temps."
        if item.get("status") != "routed":
            return "Trajet non calculé."
        return "\n".join(_leg_line(leg, tz) for leg in item.get("legs") or [])
    notes: str = item.get("notes") or ""
    return notes


NOT_RIDDEN = frozenset({"WALK", "ACCESS"})


def _title(item: dict[str, Any]) -> str:
    """A routed journey is titled by the lines to take only (e.g. ``E110 → 18``)."""
    if item["type"] != "travel" or item.get("status") != "routed":
        return str(item["title"])
    lines = [
        str(leg.get("line") or leg["mode"])
        for leg in item.get("legs") or []
        if leg["mode"] not in NOT_RIDDEN
    ]
    return " → ".join(lines) if lines else "À pied"


def _leg_line(leg: dict[str, Any], tz: ZoneInfo) -> str:
    """One journey leg, local times: the line to take, or the walk / drop-off."""
    start, end = datetime.fromisoformat(leg["start"]), datetime.fromisoformat(leg["end"])
    minutes = round((end - start).total_seconds() / 60)
    if leg["mode"] == "WALK":
        return f"À pied {minutes} min"
    if leg["mode"] == "ACCESS":
        return f"Dépôt {minutes} min"
    return (
        f"{leg.get('line') or leg['mode']} {start.astimezone(tz):%H:%M} {leg['from']} → "
        f"{end.astimezone(tz):%H:%M} {leg['to']}"
    )
