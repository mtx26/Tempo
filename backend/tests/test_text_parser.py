"""Deterministic French parser (requests and training lines)."""

from datetime import date, time

import pytest

from tempo.ai.text_parser import parse_activity_line, parse_task, parse_text, resolve_day

TODAY = date(2026, 12, 7)  # Monday


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("demain", date(2026, 12, 8)),
        ("aujourd'hui", TODAY),
        ("après-demain", date(2026, 12, 9)),
        ("vendredi", date(2026, 12, 11)),
        ("lundi", date(2026, 12, 14)),  # next occurrence, today excluded
        ("14/12", date(2026, 12, 14)),
        ("3/1", date(2027, 1, 3)),  # already passed this year -> next year
        ("14/12/2027", date(2027, 12, 14)),
        ("14-12-27", date(2027, 12, 14)),
        ("31/02", None),
        ("bientôt", None),
    ],
)
def test_resolve_day(text: str, expected: date | None) -> None:
    assert resolve_day(text, TODAY) == expected


def test_training_line_with_everything() -> None:
    a = parse_activity_line("mercredi 16/12 18h30 : Fractionné 1h05 @club - 6 x 800 m", TODAY)
    assert a is not None
    assert (a.kind, a.title, a.day, a.start) == (
        "training",
        "Fractionné",
        date(2026, 12, 16),
        time(18, 30),
    )
    assert (a.duration_minutes, a.place_id, a.notes) == (65, "club", "6 x 800 m")


def test_training_without_time_and_race_with_colon_time() -> None:
    a = parse_activity_line("lundi 14/12 : Footing 45 min - endurance fondamentale", TODAY)
    assert a is not None and a.start is None and a.notes == "endurance fondamentale"
    race = parse_activity_line("COURSE samedi 19/12 10:00 : 10 km de Namur 60 min @namur", TODAY)
    assert race is not None and race.kind == "race" and race.start == time(10, 0)
    assert race.title == "10 km de Namur"


@pytest.mark.parametrize(
    "line",
    [
        "course samedi 19/12 : 10 km 60 min",  # race without a start time
        "lundi 14/12 : footing",  # no duration
        "mardi 15/12 25h00 : footing 40 min",  # impossible time
        "footing 40 min",  # no day
        "Bonjour, voici le programme :",
    ],
)
def test_incomplete_lines_are_not_guessed(line: str) -> None:
    assert parse_activity_line(line, TODAY) is None


def test_tasks_soft_by_default_and_hard_only_when_said() -> None:
    soft = parse_task("ajoute 5h de maths pour vendredi", TODAY)
    assert soft is not None and (soft.subject, soft.effort_minutes, soft.hard) == (
        "maths",
        300,
        False,
    )
    hard = parse_task("ajoute 90 min d'analyse avant le 18/12 strict", TODAY)
    assert hard is not None and hard.hard and hard.target_date == date(2026, 12, 18)
    accents = parse_task("ajoute 2h30 de thermodynamique générale pour le 20/12", TODAY)
    assert accents is not None and accents.subject == "thermodynamique générale"
    assert accents.effort_minutes == 150
    assert parse_task("ajoute de la physique un jour", TODAY) is None


def test_parse_text_mixed_message() -> None:
    result = parse_text(
        "Salut !\n> citation ignorée\nmardi 15/12 : footing 40 min\nreplanifie\nmerci", TODAY
    )
    assert len(result.activities) == 1 and result.replan
    assert result.unparsed == ["Salut !", "merci"]
    assert result.understood
    assert not parse_text("rien de spécial", TODAY).understood


def test_sender_authentication_results() -> None:
    from email.message import EmailMessage

    from tempo.sync.training_mail import authenticated

    def msg(*results: str) -> EmailMessage:
        m = EmailMessage()
        for value in results:
            m["Authentication-Results"] = value
        return m

    mx, coach = "mx.google.com", "c@club.be"
    assert authenticated(msg("mx.google.com; dkim=pass header.i=@club.be"), coach, mx)
    assert authenticated(msg("mx.google.com; dmarc=pass (p=none) header.from=club.be"), coach, mx)
    assert authenticated(msg("MX.Google.com; dkim=pass header.d=club.be; spf=pass"), coach, mx)
    # Wrong domain, failures, nothing at all.
    assert not authenticated(msg("mx.google.com; dkim=pass header.d=evil.test"), coach, mx)
    assert not authenticated(msg("mx.google.com; dkim=pass header.d=club.be.evil"), coach, mx)
    assert not authenticated(msg("mx.google.com; dmarc=pass header.from=evil.test"), coach, mx)
    assert not authenticated(msg("mx.google.com; dmarc=fail; dkim=none"), coach, mx)
    assert not authenticated(msg(), coach, mx)
    # Forged by the sender or written by another server: never trusted.
    assert not authenticated(msg("evil.example; dmarc=pass header.from=club.be"), coach, mx)
    # The topmost trusted header decides; an older forged "pass" below it is ignored.
    forged = msg(
        "mx.google.com; dmarc=fail header.from=club.be",
        "mx.google.com; dmarc=pass header.from=club.be",
    )
    assert not authenticated(forged, coach, mx)
