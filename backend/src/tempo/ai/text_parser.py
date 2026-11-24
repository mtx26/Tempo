"""Deterministic French parser for requests and training lines (no LLM needed).

Supported (case-insensitive, accents optional):

Training / race lines (one per line, e-mails or chat)::

    lundi 14/12 : Footing 45 min - endurance fondamentale
    mercredi 16/12 18h30 : Fractionné 1h05 @club - 6 x 800 m
    COURSE samedi 19/12 10:00 : 10 km de Namur 60 min @namur
    demain 18h : footing 40 min

Requests::

    ajoute 5h de maths pour vendredi            -> study task, soft target
    ajoute 90 min d'analyse avant le 18/12 strict -> study task, hard deadline
    replanifie                                  -> re-plan request

Anything else is reported as not understood: nothing is guessed.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from tempo.text import plain as _plain

WEEKDAYS = {
    "lundi": 0,
    "mardi": 1,
    "mercredi": 2,
    "jeudi": 3,
    "vendredi": 4,
    "samedi": 5,
    "dimanche": 6,
}
HARD_WORDS = ("strict", "stricte", "imperatif", "imperative", "obligatoire", "deadline")
RACE_WORDS = ("course", "competition", "compet", "race")


@dataclass(frozen=True, slots=True)
class ParsedActivity:
    kind: str
    """``training`` or ``race``."""
    title: str
    day: date
    start: time | None
    duration_minutes: int
    place_id: str | None = None
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ParsedTask:
    subject: str
    effort_minutes: int
    target_date: date
    hard: bool


@dataclass(slots=True)
class ParseResult:
    activities: list[ParsedActivity] = field(default_factory=list)
    tasks: list[ParsedTask] = field(default_factory=list)
    replan: bool = False
    unparsed: list[str] = field(default_factory=list)

    @property
    def understood(self) -> bool:
        return bool(self.activities or self.tasks or self.replan)


_DATE = r"(?P<d>\d{1,2})[/.-](?P<m>\d{1,2})(?:[/.-](?P<y>\d{2,4}))?"
_TIME = r"(?P<hh>\d{1,2})\s*(?:h|:)\s*(?P<mm>\d{2})?"
_DURATION = re.compile(r"(?:(?P<h>\d{1,2})\s*h\s*(?P<hm>\d{2})?|(?P<min>\d{1,3})\s*min(?:utes?)?)")
_LINE = re.compile(
    r"^\s*(?P<race>(?:course|competition|compet|race)\s+)?"
    r"(?:(?P<wd>lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+)?"
    r"(?P<when>" + _DATE + r"|demain|aujourd'?hui|apres-demain)?\s*"
    r"(?:" + _TIME + r")?\s*:\s*(?P<rest>.+)$"
)
_TASK = re.compile(
    r"^\s*(?:ajoute[rz]?|planifie[rz]?|prevois)?\s*"
    r"(?:(?P<h>\d{1,3})\s*h(?:eures?)?\s*(?P<hm>\d{2})?|(?P<min>\d{1,4})\s*min(?:utes?)?)\s+"
    r"(?:de |d'|du |des )?(?P<subject>.+?)\s+(?:pour|avant)\s+(?:le\s+)?(?P<when>.+?)"
    r"(?:\s+(?P<hard>" + "|".join(HARD_WORDS) + r"))?\s*$"
)
_REPLAN = re.compile(r"^\s*(?:re-?planifie|recalcule|replan)\w*\b")


def resolve_day(text: str, today: date) -> date | None:
    """``14/12``, ``14/12/2026``, weekday names (next occurrence, today excluded),
    ``demain``... Day/month without a year means the next such date from today."""
    plain = _plain(text).strip()
    if plain in ("aujourd'hui", "aujourdhui"):
        return today
    if plain == "demain":
        return today + timedelta(days=1)
    if plain == "apres-demain":
        return today + timedelta(days=2)
    for name, index in WEEKDAYS.items():
        if plain in (name, f"{name} prochain"):
            return today + timedelta(days=(index - today.weekday() - 1) % 7 + 1)
    match = re.fullmatch(_DATE, plain)
    if match is None:
        return None
    day, month = int(match["d"]), int(match["m"])
    year = match["y"]
    try:
        if year is not None:
            full = int(year) + (2000 if len(year) == 2 else 0)
            return date(full, month, day)
        candidate = date(today.year, month, day)
        return candidate if candidate >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


def _minutes_of(match: re.Match[str]) -> int:
    if match["min"]:
        return int(match["min"])
    return int(match["h"]) * 60 + int(match["hm"] or 0)


def _duration(text: str) -> tuple[int | None, str]:
    match = _DURATION.search(text)
    if match is None:
        return None, text
    minutes = _minutes_of(match)
    rest = (text[: match.start()] + text[match.end() :]).strip()
    return (minutes if 0 < minutes <= 24 * 60 else None), rest


def parse_activity_line(line: str, today: date) -> ParsedActivity | None:
    plain = _plain(line)
    match = _LINE.match(plain)
    if match is None:
        return None
    when = match["when"]
    if when is None and match["wd"] is None:
        return None
    day = resolve_day(when, today) if when else resolve_day(match["wd"], today)
    if day is None:
        return None
    start = None
    if match["hh"] is not None:
        hours, minutes = int(match["hh"]), int(match["mm"] or 0)
        if hours > 23 or minutes > 59:
            return None
        start = time(hours, minutes)
    # Title/notes come from the ORIGINAL text (accents kept), aligned on the ':' separator.
    original_rest = line.split(":", 1)[1] if ":" in line else match["rest"]
    if match["hh"] is not None and match["mm"] is not None and line.count(":") >= 2:
        original_rest = line.split(":", 2)[2]
    head, _, notes = original_rest.partition(" - ")
    duration, head = _duration(head)
    if duration is None:
        return None
    place = None
    place_match = re.search(r"@([a-z0-9_]{1,50})", head)
    if place_match:
        place = place_match[1]
        head = (head[: place_match.start()] + head[place_match.end() :]).strip()
    title = " ".join(head.split()).strip(" ,;")
    kind = "race" if match["race"] else "training"
    if not title or (kind == "race" and start is None):
        return None
    return ParsedActivity(kind, title[:300], day, start, duration, place, notes.strip())


def parse_task(text: str, today: date) -> ParsedTask | None:
    match = _TASK.match(_plain(text))
    if match is None:
        return None
    minutes = _minutes_of(match)
    target = resolve_day(match["when"], today)
    if target is None or minutes <= 0:
        return None
    subject_plain = match["subject"].strip()
    # Recover the subject with its original accents when possible.
    original = re.search(re.escape(subject_plain), _plain(text))
    subject = text[original.start() : original.end()] if original else subject_plain
    return ParsedTask(subject.strip()[:200], minutes, target, match["hard"] is not None)


def parse_text(text: str, today: date) -> ParseResult:
    result = ParseResult()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(">"):
            continue
        if _REPLAN.match(_plain(line)):
            result.replan = True
            continue
        if (activity := parse_activity_line(line, today)) is not None:
            result.activities.append(activity)
        elif (task := parse_task(line, today)) is not None:
            result.tasks.append(task)
        else:
            result.unparsed.append(line)
    return result


def local_start(activity: ParsedActivity, tz: object) -> datetime | None:
    if activity.start is None:
        return None
    return datetime.combine(activity.day, activity.start, tzinfo=tz)  # type: ignore[arg-type]
