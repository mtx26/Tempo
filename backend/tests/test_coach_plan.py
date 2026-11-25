"""Coach weekly plan (.docx) parsing, on synthetic documents with the real structure."""

import io
import zipfile
from datetime import date

import pytest

from tempo.sync.coach_plan import PlanFormatError, parse_plan_docx, total_duration
from tests.docx_fixtures import EXAMPLE_WEEK, THEME, plan_docx

TODAY = date(2026, 11, 20)


def test_example_week_with_the_coach_structure() -> None:
    plan = parse_plan_docx(plan_docx(), TODAY)
    assert (plan.monday, plan.sunday) == (date(2026, 11, 23), date(2026, 11, 29))
    sessions = {s.weekday: s for s in plan.sessions}
    assert sorted(sessions) == [2, 4, 6]  # Monday "Repos" and empty days are not trainings
    vma, endurance, long_run = sessions[2], sessions[4], sessions[6]
    assert vma.title == "Test VMA" and vma.duration_minutes is None  # "RAC : 10 '" is a part
    assert "RAC : 10 \u2018" in vma.notes and "Préparation" in vma.notes
    assert endurance.title == "Endurance Fondamentale" and endurance.duration_minutes == 75
    assert "3x80m en déroulant" in endurance.notes
    assert long_run.duration_minutes == 85 and "accélérations en côtes" in long_run.notes
    assert [s.order for s in plan.sessions] == [0, 1, 2]
    assert vma.day == date(2026, 11, 25) and not any(s.is_race for s in plan.sessions)
    assert "VMA 17,4" not in endurance.notes  # merged banner row is not a day cell


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("Endurance fondamentale 1h15'", 75),
        ("Sortie longue 1h25\u2019", 85),
        ("Footing 45\u2019", 45),
        ("RAC : 10 \u2018", None),
        ("RAC : 10'", None),
        ("3x80m r 30\u2019\u2019", None),
        ("6 x 1000m r 2'", None),
        ("Footing 40' + 20'", None),  # two candidates: ambiguous, not summed
        ("Test VMA", None),
        ("10 km de Namur 45'", 45),  # "r" of "Namur" is not the recovery notation
        ("Fartlek (r 1') 50'", 50),
    ],
)
def test_total_duration_is_explicit_and_unique(text: str, minutes: int | None) -> None:
    assert total_duration(text) == minutes


def test_duration_from_the_theme_row_wins_and_content_needs_a_duration_only_line() -> None:
    week = {k: list(v) for k, v in EXAMPLE_WEEK.items()}
    week[THEME][4] = "Endurance fondamentale 1h05\u2019"
    week["Contenu"][6] = "Sur parcours vallonné 1h25\u2019"  # not alone on the first line
    sessions = {s.weekday: s for s in parse_plan_docx(plan_docx(week=week), TODAY).sessions}
    assert sessions[4].duration_minutes == 65
    assert sessions[6].duration_minutes is None  # to confirm, never guessed


def test_competition_is_a_race() -> None:
    week = {k: list(v) for k, v in EXAMPLE_WEEK.items()}
    week[THEME][5] = "Compétition : 10 km de Namur 45'"
    race = next(s for s in parse_plan_docx(plan_docx(week=week), TODAY).sessions if s.weekday == 5)
    assert race.is_race and race.duration_minutes == 45


def test_short_week_range_and_year_rollover() -> None:
    plan = parse_plan_docx(plan_docx(week_range="28/12 au 03/01/2027"), date(2026, 12, 20))
    assert (plan.monday, plan.sunday) == (date(2026, 12, 28), date(2027, 1, 3))
    assert plan.sessions[0].day == date(2026, 12, 30)


@pytest.mark.parametrize(
    "data",
    [
        b"not a zip",
        plan_docx(week_range="Programme de la semaine"),  # no date range
        plan_docx(week_range="24 au 30/11/2026"),  # Tuesday..Monday: refused
        plan_docx(week={k: v[:5] for k, v in EXAMPLE_WEEK.items()}),  # not 7 day columns
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
        parse_plan_docx(plan_docx(week_range="31 au 35/13/2026"), TODAY)
    big = {f"Ligne {i}": [""] * 7 for i in range(70)}
    with pytest.raises(PlanFormatError, match="rows"):
        parse_plan_docx(plan_docx(week=big), TODAY)


def test_ordinary_running_words_are_not_races() -> None:
    week = {k: list(v) for k, v in EXAMPLE_WEEK.items()}
    week[THEME][1] = "Course à pied 45'"
    week[THEME][3] = "Trail en forêt 1h"
    plan = parse_plan_docx(plan_docx(week=week), TODAY)
    assert not any(s.is_race for s in plan.sessions)
