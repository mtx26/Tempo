"""Apply a parsed coach plan (``coach_plan.WeeklyPlan``) to the database (M5b).

Update rules (docs/TRAINING_EMAILS.md):
- one row per (week, suggested weekday): ``source='coach_plan'``, ``source_ref='<monday>:<wd>'``;
- a re-sent plan updates rows in place (same id, so the planned position stays stable when the
  content did not change), adds new days and removes days the coach dropped;
- rows the user locked, edited, completed or cancelled are never overwritten or removed
  silently: the user is notified of the coach's change instead;
- sessions without an explicit total duration are stored ``status='to_confirm'`` (never
  scheduled until the user gives the duration); races always need confirmation (fixed
  commitment, start time required), even with a duration;
- days already past are skipped.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from tempo.db.models import ActivityRow
from tempo.planning.service import notify, propose
from tempo.sync.coach_plan import PlanSession, WeeklyPlan

SOURCE = "coach_plan"
PROTECTED_STATUSES = frozenset({"done", "cancelled"})


@dataclass(slots=True)
class PlanImport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    removed: list[str] = field(default_factory=list)
    to_confirm: list[str] = field(default_factory=list)
    protected_changes: list[str] = field(default_factory=list)
    skipped_past: int = 0


def _midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=tz)


def _protected(row: ActivityRow) -> bool:
    return row.locked or row.user_modified or row.status in PROTECTED_STATUSES


def _values(
    session_: PlanSession, plan: WeeklyPlan, tz: ZoneInfo, today: date
) -> dict[str, object]:
    needs_confirmation = session_.duration_minutes is None or session_.is_race
    earliest = max(_midnight(plan.monday, tz), _midnight(today, tz))
    latest = _midnight(plan.sunday + timedelta(days=1), tz)
    if session_.is_race:  # the race day is fixed; its start time is confirmed by the user
        earliest = _midnight(session_.day, tz)
        latest = _midnight(session_.day + timedelta(days=1), tz)
    return {
        "kind": "race" if session_.is_race else "training",
        "title": session_.title,
        "notes": session_.notes,
        "duration_minutes": session_.duration_minutes,
        "earliest_start_at": earliest,
        "latest_end_at": latest,
        "preferred_day": session_.day,
        "sequence_group": f"{SOURCE}:{plan.monday.isoformat()}",
        "sequence_index": session_.order,
        "status": "to_confirm" if needs_confirmation else "active",
    }


def _json_value(value: object) -> object:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def apply_weekly_plan(
    session: Session, plan: WeeklyPlan, *, tz: ZoneInfo, today: date, origin: str
) -> PlanImport:
    report = PlanImport()
    prefix = f"{plan.monday.isoformat()}:"
    existing = {
        row.source_ref: row
        for row in session.scalars(
            select(ActivityRow).where(
                ActivityRow.source == SOURCE, ActivityRow.source_ref.like(prefix + "%")
            )
        )
    }
    wanted: set[str] = set()
    for item in plan.sessions:
        if item.day < today:
            report.skipped_past += 1
            continue
        ref = f"{prefix}{item.weekday}"
        wanted.add(ref)
        values = _values(item, plan, tz, today)
        label = f"{item.title} ({item.day.strftime('%d/%m')})"
        row = existing.get(ref)
        if row is None:
            session.add(ActivityRow(source=SOURCE, source_ref=ref, **values))
            report.created.append(label)
        elif _protected(row):
            if (row.title, row.notes, row.duration_minutes) != (
                values["title"],
                values["notes"],
                values["duration_minutes"],
            ):
                report.protected_changes.append(label)
                propose(
                    session,
                    "coach_plan_change",
                    f"Le coach a modifié « {item.title} » ({item.day.strftime('%d/%m')})",
                    {
                        "activity_id": str(row.id),
                        "values": {k: _json_value(v) for k, v in values.items()},
                        "previous": {"title": row.title, "notes": row.notes},
                    },
                    f"coach_plan_change:{row.id}:{origin}",
                )
            continue
        elif all(getattr(row, key) == value for key, value in values.items()):
            report.unchanged += 1
        else:
            for key, value in values.items():
                setattr(row, key, value)
            report.updated.append(label)
        if values["status"] == "to_confirm":
            report.to_confirm.append(label)
    for existing_ref, row in existing.items():
        if existing_ref in wanted:
            continue
        if row.preferred_day is not None and row.preferred_day < today:
            continue  # history of past days is kept
        if _protected(row):
            report.protected_changes.append(f"{row.title} (retirée du plan)")
            continue
        session.delete(row)
        report.removed.append(row.title)
    session.flush()
    _notify(session, plan, report, origin)
    return report


def _notify(session: Session, plan: WeeklyPlan, report: PlanImport, origin: str) -> None:
    week = f"semaine du {plan.monday.strftime('%d/%m')} au {plan.sunday.strftime('%d/%m/%Y')}"
    parts = []
    if report.created:
        parts.append("ajoutées : " + ", ".join(report.created))
    if report.updated:
        parts.append("mises à jour : " + ", ".join(report.updated))
    if report.removed:
        parts.append("retirées : " + ", ".join(report.removed))
    if parts:
        notify(
            session,
            "coach_plan",
            f"Plan d'entraînement importé ({week})",
            "Séances " + " ; ".join(parts) + ". Les jours proposés par le coach sont des "
            "suggestions : Tempo peut les déplacer en respectant l'ordre et la récupération.",
            f"coach_plan:{origin}",
        )
    if report.to_confirm:
        notify(
            session,
            "coach_plan_confirm",
            "Séances à confirmer",
            "Durée (et heure de départ pour une compétition) à préciser avant planification : "
            + ", ".join(report.to_confirm),
            f"coach_plan_confirm:{origin}",
        )
    if report.protected_changes:
        notify(
            session,
            "coach_plan_conflict",
            "Plan du coach modifié sur des séances protégées",
            "Le coach a changé des séances que vous avez verrouillées, modifiées ou terminées ; "
            "elles n'ont pas été écrasées. Une proposition à approuver est créée pour chacune : "
            + ", ".join(report.protected_changes),
            f"coach_plan_conflict:{origin}",
        )
