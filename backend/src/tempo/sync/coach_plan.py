"""Weekly training plan from the coach's ``.docx`` attachment (M5b, ARCHITECTURE.md).

Format: a date range ``Semaine du 23 au 29/11/2026`` and one table whose header row holds the
weekdays (Monday..Sunday) and whose other rows (objective, theme, detailed instructions) hold the
session of each day; empty columns mean no training.

Rules implemented here (deterministic, nothing invented):
- the full text of every row is kept verbatim in the session notes;
- the total duration is read ONLY from the session/theme row (where the coach writes it, e.g.
  ``Endurance fondamentale 1h15'``) and only when exactly one explicit duration is there.
  Durations in the detailed instructions belong to parts of the session (``RAC : 10'``, ``r 30"``,
  ``20' footing``, repetitions) and are never summed or promoted to a total. Otherwise the
  duration is None: "to confirm" by the user;
- a session whose theme says rest (``Repos``) is not a training;
- a competition keyword (course, compétition, cross, semi, marathon, trail) marks a race; it is
  never scheduled without the user's approval (fixed commitment).
"""

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from docx import Document
from docx.table import Table

MAX_DOCX_BYTES = 2_000_000
MAX_TABLES = 10
MAX_ROWS = 60
MAX_COLUMNS = 20
MAX_PARAGRAPHS = 500
MAX_UNCOMPRESSED = 20_000_000
WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
REST_WORDS = ("repos", "off", "rien")
RACE_WORDS = (
    "competition",
    "compet ",
    "cross",
    "semi-marathon",
    "marathon",
    "dossard",
    "championnat",
)
"""Unambiguous race markers only ("course", "trail" or "10 km" often describe ordinary training)."""
PART_PREFIX = re.compile(
    r"(?:^|[\s(,;+])(rac|retour au calme|echauffement|echauf|ech\.?|r|recup|recuperation|x)"
    r"\s*[:=]?\s*$"
)
_DURATION = re.compile(r"(?<![\d.])(\d{1,2})\s*h\s*(\d{2})?\s*'?|(?<![\dx.])(\d{1,3})\s*'(?!')")
_WEEK = re.compile(
    r"semaine\s+du\s+(?P<d1>\d{1,2})(?:[/.](?P<m1>\d{1,2}))?(?:[/.](?P<y1>\d{4}))?"
    r"\s+au\s+(?P<d2>\d{1,2})[/.](?P<m2>\d{1,2})[/.](?P<y2>\d{4})"
)


class PlanFormatError(ValueError):
    """The attachment is not a readable weekly plan (reported to the user, nothing imported)."""


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


@dataclass(frozen=True, slots=True)
class PlanSession:
    weekday: int
    """0 = Monday: the coach's suggestion, not a commitment."""
    day: date
    title: str
    notes: str
    duration_minutes: int | None
    """None = no single explicit total duration: to be confirmed by the user."""
    is_race: bool
    order: int
    """Position of the session in the week (recovery/sequence constraints)."""


@dataclass(slots=True)
class WeeklyPlan:
    monday: date
    sunday: date
    sessions: list[PlanSession] = field(default_factory=list)


def total_duration(text: str) -> int | None:
    """The single explicit total duration in ``text`` (minutes), else None."""
    totals: set[int] = set()
    plain = _plain(text)
    for match in _DURATION.finditer(plain):
        prefix = plain[max(0, match.start() - 25) : match.start()]
        if PART_PREFIX.search(prefix) or re.search(r"\d\s*x\s*$", prefix):
            continue  # part of the session (warm-up, recovery, repetitions)
        hours_form = match[3] is None
        minutes = int(match[1]) * 60 + int(match[2] or 0) if hours_form else int(match[3])
        if 0 < minutes <= 6 * 60:
            totals.add(minutes)
    return totals.pop() if len(totals) == 1 else None


def _check_archive(data: bytes) -> None:
    if len(data) > MAX_DOCX_BYTES:
        raise PlanFormatError("attachment larger than 2 MB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_UNCOMPRESSED:
                raise PlanFormatError("attachment expands too much (zip bomb guard)")
    except zipfile.BadZipFile as exc:
        raise PlanFormatError("attachment is not a .docx file") from exc


def _week(texts: list[str], today: date) -> tuple[date, date]:
    for text in texts:
        match = _WEEK.search(_plain(text))
        if match is None:
            continue
        year, month = int(match["y2"]), int(match["m2"])
        end = date(year, month, int(match["d2"]))
        start_month = int(match["m1"] or month)
        start_year = int(match["y1"] or (year - 1 if start_month > month else year))
        start = date(start_year, start_month, int(match["d1"]))
        if start.weekday() != 0 or (end - start).days != 6:
            raise PlanFormatError(f"week range {start}..{end} is not Monday..Sunday")
        return start, end
    raise PlanFormatError("no 'Semaine du ... au ...' range found")


def _weekday_columns(grid: list[list[str]]) -> tuple[int, dict[int, int]] | None:
    for row_index, row in enumerate(grid):
        columns: dict[int, int] = {}
        for column_index, cell in enumerate(row):
            name = _plain(cell).strip()
            if name in WEEKDAYS:
                columns[column_index] = WEEKDAYS.index(name)
        if len(columns) >= 5:
            return row_index, columns
    return None


def parse_plan_docx(data: bytes, today: date) -> WeeklyPlan:
    """Parse a coach plan; ANY malformed content raises ``PlanFormatError`` (reported to the
    user as unreadable, never a crash that would silently drop the e-mail)."""
    _check_archive(data)
    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:  # python-docx raises several types for corrupt files
        raise PlanFormatError("unreadable .docx") from exc
    try:
        return _parse_document(document, today)
    except PlanFormatError:
        raise
    except (ValueError, IndexError, KeyError, AttributeError, TypeError) as exc:
        raise PlanFormatError(f"unexpected plan layout ({type(exc).__name__})") from exc


def _rows(table: Table) -> list[list[str]]:
    """Cell texts per row, computed once per row (``row.cells`` is costly), size-capped."""
    rows = table.rows
    if len(rows) > MAX_ROWS:
        raise PlanFormatError(f"table has more than {MAX_ROWS} rows")
    return [[cell.text for cell in row.cells][:MAX_COLUMNS] for row in rows]


def _parse_document(document: Any, today: date) -> WeeklyPlan:
    tables = list(document.tables)[:MAX_TABLES]
    grids = [_rows(table) for table in tables]
    texts = [p.text for p in document.paragraphs[:MAX_PARAGRAPHS]]
    for grid in grids:
        texts += [cell for row in grid for cell in row]
    monday, sunday = _week(texts, today)
    for grid in grids:
        found = _weekday_columns(grid)
        if found is None:
            continue
        header_row, columns = found
        cells: dict[int, list[tuple[str, str]]] = {d: [] for d in range(7)}
        for row in grid[header_row + 1 :]:
            label = row[0].strip() if row else ""
            for column_index, weekday in columns.items():
                value = row[column_index].strip() if column_index < len(row) else ""
                if value:
                    cells[weekday].append((label, value))
        plan = WeeklyPlan(monday, sunday)
        order = 0
        for weekday in range(7):
            entries = cells[weekday]
            if not entries:
                continue
            body = "\n".join(f"{label} : {value}" if label else value for label, value in entries)
            plain = _plain(body)
            values = [value for _, value in entries]
            theme = next(
                (v for label, v in entries if _plain(label).startswith(("seance", "theme"))),
                values[0],
            )
            first_line = theme.splitlines()[0].strip()
            if any(re.fullmatch(rf"\s*{w}\s*", _plain(first_line)) for w in REST_WORDS):
                continue
            plan.sessions.append(
                PlanSession(
                    weekday=weekday,
                    day=monday + timedelta(days=weekday),
                    title=first_line[:300],
                    notes=body[:10_000],
                    duration_minutes=total_duration(theme),
                    is_race=any(word in plain for word in RACE_WORDS),
                    order=order,
                )
            )
            order += 1
        return plan
    raise PlanFormatError("no weekly table with Monday..Sunday columns found")
