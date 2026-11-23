from datetime import UTC, date, datetime, timedelta

import pytest

from tempo.domain.models import FixedEvent, StudyTask, TargetKind


def aware(hour: int) -> datetime:
    return datetime(2026, 12, 1, hour, tzinfo=UTC)


def test_study_task_target_is_soft_by_default() -> None:
    task = StudyTask(subject="Analyse", effort_minutes=600, target_date=date(2027, 1, 15))
    assert task.target_kind is TargetKind.SOFT


def test_remaining_minutes_subtracts_completed() -> None:
    task = StudyTask(
        subject="Analyse", effort_minutes=600, completed_minutes=150, target_date=date(2027, 1, 15)
    )
    assert task.remaining_minutes == 450


def test_remaining_minutes_never_negative() -> None:
    task = StudyTask(
        subject="Analyse", effort_minutes=60, completed_minutes=90, target_date=date(2027, 1, 15)
    )
    assert task.remaining_minutes == 0


@pytest.mark.parametrize("effort", [0, -30])
def test_study_task_requires_positive_effort(effort: int) -> None:
    with pytest.raises(ValueError):
        StudyTask(subject="Analyse", effort_minutes=effort, target_date=date(2027, 1, 15))


def test_fixed_event_requires_timezone_aware_datetimes() -> None:
    with pytest.raises(ValueError):
        FixedEvent(
            uid="a",
            start=datetime(2026, 12, 1, 8),
            end=datetime(2026, 12, 1, 10),
            summary="Cours",
        )


def test_fixed_event_requires_end_after_start() -> None:
    with pytest.raises(ValueError):
        FixedEvent(uid="a", start=aware(10), end=aware(10), summary="Cours")


def test_fixed_event_duration() -> None:
    event = FixedEvent(uid="a", start=aware(8), end=aware(10), summary="Cours")
    assert event.duration == timedelta(hours=2)


def test_study_task_rejects_negative_completed_minutes() -> None:
    with pytest.raises(ValueError):
        StudyTask(
            subject="Analyse",
            effort_minutes=60,
            completed_minutes=-1,
            target_date=date(2027, 1, 15),
        )
