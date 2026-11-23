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
from tempo.db.models import ActivityRow, PlaceRow, StudyTaskRow
from tempo.domain.models import TargetKind

MAX_EFFORT = 100_000
TRAINING_SLACK = timedelta(days=1)
"""A training without explicit window may move one day earlier or later (flexible)."""


@dataclass(slots=True)
class Applied:
    created: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def _midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=tz)


def activity_values(
    item: ParsedActivity, tz: ZoneInfo, today: date, known_places: set[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """Validated column values for an ``activity`` row, or a rejection reason."""
    if item.day < today:
        return None, f"{item.title} : date passée ({item.day.isoformat()})"
    if not 0 < item.duration_minutes <= 24 * 60:
        return None, f"{item.title} : durée invalide"
    place = item.place_id if item.place_id in known_places else None
    start = None if item.start is None else datetime.combine(item.day, item.start, tzinfo=tz)
    duration = timedelta(minutes=item.duration_minutes)
    if item.kind == "race":
        if start is None:
            return None, f"{item.title} : une course doit avoir une heure de départ"
        earliest, latest = start, start + duration
    else:
        earliest = max(_midnight(item.day, tz) - TRAINING_SLACK, _midnight(today, tz))
        latest = _midnight(item.day + timedelta(days=1), tz) + TRAINING_SLACK
    values = {
        "kind": item.kind,
        "title": item.title[:300],
        "notes": item.notes[:10_000],
        "duration_minutes": item.duration_minutes,
        "earliest_start_at": earliest,
        "latest_end_at": latest,
        "preferred_start_at": start if item.kind == "training" else None,
        "place_id": place,
    }
    return values, None


def task_values(item: ParsedTask, today: date) -> tuple[dict[str, Any] | None, str | None]:
    if not 0 < item.effort_minutes <= MAX_EFFORT:
        return None, f"{item.subject} : effort invalide"
    if item.target_date < today:
        return None, f"{item.subject} : échéance passée"
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
    places = known_places(session)
    for task in result.tasks:
        values, reason = task_values(task, today)
        if values is None:
            outcome.rejected.append(reason or "tâche invalide")
            continue
        session.add(StudyTaskRow(**values))
        kind = "stricte" if task.hard else "souple"
        outcome.created.append(
            f"tâche « {task.subject} » {task.effort_minutes} min, échéance {kind} "
            f"{task.target_date.isoformat()}"
        )
    for index, activity in enumerate(result.activities):
        values, reason = activity_values(activity, tz, today, places)
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
