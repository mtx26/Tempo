"""Turn interpreted text (deterministic parser or LLM) into validated domain changes.

Every item is re-validated here exactly like an API request (positive durations, dates not in
the past, races with a fixed start, known places only). Invalid items are reported, never
"fixed". Insertions are idempotent through ``activity(source, source_ref)``.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tempo.ai.llm import Extraction
from tempo.ai.text_parser import ParsedActivity, ParsedTask, ParseResult
from tempo.db.models import ActivityRow, PlaceRow
from tempo.domain.models import TargetKind
from tempo.timeutil import local_midnight

MAX_EFFORT = 100_000
MAX_ACTIVITY_AHEAD = timedelta(days=60)
"""Trainings/races further ahead are refused (also bounds solver input and date arithmetic)."""
MAX_TASK_AHEAD = timedelta(days=366)
MAX_ITEMS = 20
"""Items applied per request/e-mail; more is refused (a forged e-mail must not flood the plan)."""


@dataclass(slots=True)
class Applied:
    created: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def activity_values(
    item: ParsedActivity,
    tz: ZoneInfo,
    today: date,
    known_places: set[str],
    *,
    suggested_day: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validated column values for an ``activity`` row, or a rejection reason.

    A training's day is either the user's own choice (conversation: the session moves within
    that day) or a coach suggestion (e-mail: like the weekly plan, the session may move within
    its week and the suggested day is a soft preference)."""
    if item.day < today:
        return None, f"{item.title} : date passée ({item.day.isoformat()})"
    if item.day > today + MAX_ACTIVITY_AHEAD:
        return None, f"{item.title} : date trop lointaine (plus de 60 jours)"
    if not 0 < item.duration_minutes <= 24 * 60:
        return None, f"{item.title} : durée invalide"
    place = item.place_id if item.place_id in known_places else None
    start = None if item.start is None else datetime.combine(item.day, item.start, tzinfo=tz)
    duration = timedelta(minutes=item.duration_minutes)
    if item.kind == "race":
        if start is None:
            return None, f"{item.title} : une course doit avoir une heure de départ"
        earliest, latest = start, start + duration
    elif suggested_day:
        monday = item.day - timedelta(days=item.day.weekday())
        earliest = max(local_midnight(monday, tz), local_midnight(today, tz))
        latest = local_midnight(monday + timedelta(days=7), tz)
    else:
        earliest = max(local_midnight(item.day, tz), local_midnight(today, tz))
        latest = local_midnight(item.day + timedelta(days=1), tz)
    values = {
        "kind": item.kind,
        "title": item.title[:300],
        "notes": item.notes[:10_000],
        "duration_minutes": item.duration_minutes,
        "earliest_start_at": earliest,
        "latest_end_at": latest,
        "preferred_start_at": start if item.kind == "training" else None,
        "preferred_day": item.day if suggested_day and item.kind == "training" else None,
        "place_id": place,
    }
    return values, None


def task_values(item: ParsedTask, today: date) -> tuple[dict[str, Any] | None, str | None]:
    if not 0 < item.effort_minutes <= MAX_EFFORT:
        return None, f"{item.subject} : effort invalide"
    if item.target_date < today:
        return None, f"{item.subject} : échéance passée"
    if item.target_date > today + MAX_TASK_AHEAD:
        return None, f"{item.subject} : échéance à plus d'un an"
    return {
        "subject": item.subject[:200],
        "effort_minutes": item.effort_minutes,
        "target_date": item.target_date,
        "target_kind": TargetKind.HARD if item.hard else TargetKind.SOFT,
    }, None


def known_places(session: Session) -> set[str]:
    return set(session.scalars(select(PlaceRow.id)))


def apply_result(
    session: Session,
    result: ParseResult,
    *,
    tz: ZoneInfo,
    today: date,
    source: str,
    source_ref: str | None = None,
) -> Applied:
    outcome = Applied()
    if len(result.tasks) + len(result.activities) > MAX_ITEMS:
        outcome.rejected.append(
            f"plus de {MAX_ITEMS} éléments dans un seul message : rien appliqué"
        )
        return outcome
    places = known_places(session)
    outcome.rejected += [
        f"{task.subject} : une tâche d'étude ne vient jamais d'un e-mail" for task in result.tasks
    ]
    for index, activity in enumerate(result.activities):
        values, reason = activity_values(activity, tz, today, places, suggested_day=True)
        if values is None:
            outcome.rejected.append(reason or "activité invalide")
            continue
        ref = None if source_ref is None else f"{source_ref}#{index}"
        stmt = insert(ActivityRow).values(**values, source=source, source_ref=ref)
        inserted = session.execute(
            stmt.on_conflict_do_nothing(index_elements=["source", "source_ref"]).returning(
                ActivityRow.id
            )
        ).first()
        if inserted is None:
            continue  # already imported (same e-mail line)
        label = "course" if activity.kind == "race" else "entraînement"
        where = " (lieu inconnu)" if activity.place_id and not values["place_id"] else ""
        outcome.created.append(
            f"{label} « {activity.title} » le {activity.day.isoformat()}"
            f"{' à ' + activity.start.strftime('%H:%M') if activity.start else ''}"
            f", {activity.duration_minutes} min{where}"
        )
    session.flush()
    return outcome


def from_extraction(extraction: Extraction) -> tuple[ParseResult, list[str]]:
    """Convert LLM output to the parser's types; malformed fields are rejected, not repaired."""
    result = ParseResult(replan=extraction.replan, unparsed=list(extraction.not_understood))
    rejected: list[str] = []
    for item in extraction.items:
        try:
            day = date.fromisoformat(item.day)
            start = None if item.start_time is None else time.fromisoformat(item.start_time)
        except ValueError:
            rejected.append(f"{item.title} : date ou heure illisible")
            continue
        if item.kind == "study_task":
            result.tasks.append(ParsedTask(item.title, item.minutes, day, item.hard_deadline))
        else:
            result.activities.append(
                ParsedActivity(
                    item.kind, item.title, day, start, item.minutes, item.place_id, item.notes
                )
            )
    return result, rejected


def result_to_json(result: ParseResult) -> dict[str, Any]:
    return {
        "tasks": [
            {
                "subject": t.subject,
                "effort_minutes": t.effort_minutes,
                "target_date": t.target_date.isoformat(),
                "hard": t.hard,
            }
            for t in result.tasks
        ],
        "activities": [
            {
                "kind": a.kind,
                "title": a.title,
                "day": a.day.isoformat(),
                "start": None if a.start is None else a.start.isoformat(),
                "duration_minutes": a.duration_minutes,
                "place_id": a.place_id,
                "notes": a.notes,
            }
            for a in result.activities
        ],
    }


def result_from_json(data: dict[str, Any]) -> ParseResult:
    return ParseResult(
        tasks=[
            ParsedTask(
                t["subject"], t["effort_minutes"], date.fromisoformat(t["target_date"]), t["hard"]
            )
            for t in data.get("tasks", [])
        ],
        activities=[
            ParsedActivity(
                a["kind"],
                a["title"],
                date.fromisoformat(a["day"]),
                None if a["start"] is None else time.fromisoformat(a["start"]),
                a["duration_minutes"],
                a["place_id"],
                a["notes"],
            )
            for a in data.get("activities", [])
        ],
    )
