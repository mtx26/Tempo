"""Deterministic answers to schedule questions (conversational assistant, no LLM needed).

Supported (French, accents optional):
- "qu'est-ce que j'ai demain / aujourd'hui / vendredi / le 14/12 ?" -> that day's agenda
- "prochain départ ?" / "quand dois-je partir ?"                  -> next departure
- "prochain cours / prochain rendez-vous / c'est quoi la suite ?"   -> next commitment
- "combien d'heures d'étude il me reste ?"                         -> remaining study effort
Answers are built only from stored data; nothing is invented ("rien de prévu" when empty).
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from tempo.ai.text_parser import resolve_day
from tempo.planning.views import schedule_items, summary
from tempo.text import plain

TYPE_LABELS = {
    "class": "cours",
    "study": "étude",
    "training": "entraînement",
    "race": "compétition",
    "personal": "rendez-vous",
    "task": "tâche",
    "travel": "trajet",
}


def _plain(text: str) -> str:
    return plain(text).strip()


@dataclass(frozen=True, slots=True)
class Question:
    kind: str
    """``agenda``, ``departure``, ``next``, ``remaining``."""
    day: date | None = None


AGENDA_TRIGGER = re.compile(
    r"(qu'?est[- ]ce que j'?ai|j'?ai quoi|programme|planning|agenda|emploi du temps)"
)
DAY_WORDS = re.compile(
    r"(apres-demain|aujourd'?hui|demain|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
)


def parse_question(text: str, today: date) -> Question | None:
    plain = _plain(text).rstrip(" ?!.")
    if re.search(
        r"\b(prochain depart|quand (?:dois[- ]je|faut[- ]il) partir|partir quand)\b", plain
    ):
        return Question("departure")
    if re.search(
        r"\b(prochain(?:e)? (?:cours|rendez-vous|evenement|seance|activite)|la suite)\b", plain
    ):
        return Question("next")
    if re.search(r"\b(combien|reste)\b.*\b(etude|etudier|travail|heures?)\b", plain):
        return Question("remaining")
    if AGENDA_TRIGGER.search(plain):
        found = DAY_WORDS.search(plain)
        day = resolve_day(found[1], today) if found else today
        if day is not None:
            return Question("agenda", day)
    return None


def _hhmm(value: str, tz: ZoneInfo) -> str:
    return datetime.fromisoformat(value).astimezone(tz).strftime("%H:%M")


def _describe(item: dict[str, Any], tz: ZoneInfo) -> str:
    label = TYPE_LABELS.get(item["type"], item["type"])
    where = f" ({item['location']})" if item.get("location") else ""
    return f"{_hhmm(item['start'], tz)}-{_hhmm(item['end'], tz)} {label} : {item['title']}{where}"


def answer(session: Session, question: Question, now: datetime, tz: ZoneInfo) -> str:
    if question.kind == "agenda" and question.day is not None:
        start = datetime.combine(question.day, time(0), tzinfo=tz)
        items = [i for i in schedule_items(session, start, start + timedelta(days=1))]
        label = question.day.strftime("%d/%m")
        if not items:
            return f"Rien de prévu le {label}."
        return f"Le {label} : " + " ; ".join(_describe(i, tz) for i in items) + "."
    data = summary(session, now, tz)
    if question.kind == "departure":
        check = data.get("departure_check") or {}
        trip = data.get("next_departure")
        if check.get("leave_at"):
            return (
                f"Départ conseillé à {_hhmm(check['leave_at'], tz)} pour "
                f"{check.get('event_title', 'votre prochain engagement')}"
                f" (état : {check.get('status', 'inconnu')})."
            )
        if trip:
            departure = trip.get("departure") or trip["start"]
            return f"Prochain départ à {_hhmm(departure, tz)} : {trip['title']}."
        return "Aucun départ prévu dans les prochains jours."
    if question.kind == "next":
        item = data.get("next_event")
        if not item:
            return "Rien de prévu dans les 7 prochains jours."
        day = datetime.fromisoformat(item["start"]).astimezone(tz).strftime("%d/%m")
        return f"Prochain : le {day}, {_describe(item, tz)}."
    hours, minutes = divmod(int(data["remaining_study_minutes"]), 60)
    unplanned = int(data.get("unplanned_minutes") or 0)
    text = f"Il reste {hours} h {minutes:02d} d'étude à faire."
    if unplanned:
        text += f" Attention : {unplanned} min ne trouvent pas de place avant leur échéance."
    return text
