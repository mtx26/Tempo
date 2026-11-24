"""Coach weekly plan (.docx) parsing on synthetic fixtures (docs/TRAINING_EMAILS.md)."""

import io
import zipfile
from datetime import date

import pytest

from tempo.sync.coach_plan import PlanFormatError, parse_plan_docx, total_duration
from tests.docx_fixtures import EXAMPLE_WEEK, plan_docx

TODAY = date(2026, 11, 20)


def test_example_week_from_the_documented_plan() -> None:
    plan = parse_plan_docx(plan_docx(), TODAY)
    assert (plan.monday, plan.sunday) == (date(2026, 11, 23), date(2026, 11, 29))
    sessions = {s.weekday: s for s in plan.sessions}
    assert sorted(sessions) == [2, 4, 6]  # Monday "Repos" and empty days are not trainings
    vma, endurance, long_run = sessions[2], sessions[4], sessions[6]
    assert vma.title == "Test VMA" and vma.duration_minutes is None  # never invented
    assert "RAC : 10'" in vma.notes and "Évaluer la VMA" in vma.notes
    assert endurance.duration_minutes == 75
    assert '3x80m r 30"' in endurance.notes
    assert long_run.duration_minutes == 85 and "Accélérations en côte" in long_run.notes
    assert [s.order for s in plan.sessions] == [0, 1, 2]
    assert vma.day == date(2026, 11, 25) and not any(s.is_race for s in plan.sessions)


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("Endurance fondamentale 1h15'", 75),
        ("Sortie longue 1h25'", 85),
        ("Footing 45'", 45),
        ("RAC : 10'", None),
        ('3x80m r 30"', None),
        ("6 x 1000m r 2'", None),
        ("Footing 40' + 20'", None),  # two candidates: ambiguous, not summed
        ("Test VMA", None),
        ("10 km de Namur 45'", 45),  # "r" of "Namur" is not the recovery notation
        ("Fartlek (r 1') 50'", 50),
    ],
)
def test_total_duration_is_explicit_and_unique(text: str, minutes: int | None) -> None:
    assert total_duration(text) == minutes


def test_week_title_inside_the_table_and_competition() -> None:
    week = {k: list(v) for k, v in EXAMPLE_WEEK.items()}
    week["Séance"][5] = "Compétition : 10 km de Namur 45'"
    plan = parse_plan_docx(plan_docx(week=week, title_in_table=True), TODAY)
    race = next(s for s in plan.sessions if s.weekday == 5)
    assert race.is_race and race.duration_minutes == 45


def test_short_week_range_and_year_rollover() -> None:
    plan = parse_plan_docx(plan_docx(title="Semaine du 28/12 au 03/01/2027"), date(2026, 12, 20))
    assert (plan.monday, plan.sunday) == (date(2026, 12, 28), date(2027, 1, 3))
    assert plan.sessions[0].day == date(2026, 12, 30)


@pytest.mark.parametrize(
    "data",
    [
        b"not a zip",
        plan_docx(title="Programme de la semaine"),  # no date range
        plan_docx(title="Semaine du 24 au 30/11/2026"),  # Tuesday..Monday: refused
        plan_docx(days=["A", "B", "C", "D", "E", "F", "G"]),  # no weekday header
    ],
)
def test_unreadable_plans_are_rejected(data: bytes) -> None:
    with pytest.raises(PlanFormatError):
        parse_plan_docx(data, TODAY)


def test_zip_bomb_and_size_guards() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"0" * 30_000_000)
    with pytest.raises(PlanFormatError, match="expands"):
        parse_plan_docx(buffer.getvalue(), TODAY)
    with pytest.raises(PlanFormatError, match="2 MB"):
        parse_plan_docx(b"x" * 2_000_001, TODAY)


def test_impossible_dates_and_oversized_tables_are_unreadable_not_crashes() -> None:
    with pytest.raises(PlanFormatError):
        parse_plan_docx(plan_docx(title="Semaine du 31 au 35/13/2026"), TODAY)
    big = {f"Ligne {i}": [""] * 7 for i in range(70)}
    with pytest.raises(PlanFormatError, match="rows"):
        parse_plan_docx(plan_docx(week=big), TODAY)
