"""Flexible training placement (trainings move, keep duration; travel reserved)."""

import itertools
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tempo.scheduler.activities import FlexibleActivity, plan_activities
from tempo.timeutil import Interval

TZ = ZoneInfo("Europe/Brussels")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 12, day, hour, minute, tzinfo=TZ)


def run(day_from: int = 1, day_to: int = 3, **kw: object) -> FlexibleActivity:
    return FlexibleActivity(
        activity_id=str(kw.get("id", "run")),
        title="Footing",
        duration=timedelta(minutes=60),
        earliest_start=at(day_from, 0),
        latest_end=at(day_to, 23, 59),
        **{k: v for k, v in kw.items() if k != "id"},  # type: ignore[arg-type]
    )


def test_preferred_start_is_used_when_free() -> None:
    plan = plan_activities([run(preferred_start=at(2, 18))], [], TZ)
    assert plan.placed[0].start == at(2, 18)
    assert plan.placed[0].end == at(2, 19)


def test_conflict_moves_the_session_keeping_its_duration_and_travel() -> None:
    busy = [Interval(at(2, 17), at(2, 20))]
    item = run(
        preferred_start=at(2, 18),
        travel_before=timedelta(minutes=20),
        travel_after=timedelta(minutes=25),
    )
    placed = plan_activities([item], busy, TZ).placed[0]
    assert placed.end - placed.start == timedelta(minutes=60)
    assert not placed.reserved.overlaps(busy[0])
    assert placed.reserved.start == placed.start - timedelta(minutes=20)
    assert placed.reserved.end == placed.end + timedelta(minutes=25)


def test_session_stays_within_activity_hours_and_window() -> None:
    placed = plan_activities([run(preferred_start=at(2, 3))], [], TZ).placed[0]
    assert placed.start.hour >= 6 and placed.end <= at(placed.start.day, 22)


def test_no_slot_is_reported_not_forced() -> None:
    busy = [Interval(at(1, 0), at(4, 0))]
    plan = plan_activities([run()], busy, TZ)
    assert plan.unplaced == ["run"]
    assert plan.placed == []


def test_window_shorter_than_duration_is_unplaced() -> None:
    item = FlexibleActivity("x", "x", timedelta(hours=2), at(2, 10), at(2, 11))
    assert plan_activities([item], [], TZ).unplaced == ["x"]


def test_two_sessions_never_overlap() -> None:
    a = run(id="a", preferred_start=at(2, 18))
    b = run(id="b", preferred_start=at(2, 18))
    placed = plan_activities([a, b], [], TZ).placed
    assert len(placed) == 2
    assert not placed[0].reserved.overlaps(placed[1].reserved)


def test_previous_placement_beats_preference() -> None:
    item = run(preferred_start=at(2, 18), previous_start=at(3, 7))
    assert plan_activities([item], [], TZ).placed[0].start == at(3, 7)


def test_empty_input() -> None:
    assert plan_activities([], [], TZ).placed == []


# --- coach plan week: order, recovery days, suggested day ---


def week_session(index: int, day: int, rest: int, minutes: int = 60) -> FlexibleActivity:
    return FlexibleActivity(
        activity_id=f"s{index}",
        title=f"séance {index}",
        duration=timedelta(minutes=minutes),
        earliest_start=datetime(2026, 12, 7, 0, 0, tzinfo=TZ),
        latest_end=datetime(2026, 12, 14, 0, 0, tzinfo=TZ),
        preferred_day=date(2026, 12, day),
        sequence_group="week-2026-12-07",
        sequence_index=index,
        min_days_after_previous=rest,
    )


WEEK = [week_session(0, 9, 0), week_session(1, 11, 2), week_session(2, 13, 2, 85)]


def placed_days(busy: list[Interval]) -> tuple[list[date], list[str]]:
    result = plan_activities(WEEK, busy, TZ)
    by_id = {p.activity_id: p.start.date() for p in result.placed}
    return [by_id[k] for k in ("s0", "s1", "s2") if k in by_id], result.unplaced


def assert_order_and_recovery(days: list[date]) -> None:
    assert days == sorted(days)
    assert all((b - a).days >= 2 for a, b in itertools.pairwise(days))


def test_suggested_days_are_kept_when_free() -> None:
    days, missing = placed_days([])
    assert missing == []
    assert [d.day for d in days] == [9, 11, 13]


def test_blocked_day_reorganises_the_week_without_breaking_recovery() -> None:
    friday = Interval(datetime(2026, 12, 11, 0, tzinfo=TZ), datetime(2026, 12, 12, 0, tzinfo=TZ))
    days, missing = placed_days([friday])
    assert missing == []
    assert date(2026, 12, 11) not in days
    assert_order_and_recovery(days)


def test_impossible_recovery_is_reported_not_violated() -> None:
    blocked = [
        Interval(datetime(2026, 12, 7, 0, tzinfo=TZ), datetime(2026, 12, 9, 0, tzinfo=TZ)),
        Interval(datetime(2026, 12, 10, 0, tzinfo=TZ), datetime(2026, 12, 13, 0, tzinfo=TZ)),
    ]  # only Wednesday 9 and Sunday 13 free: three sessions cannot keep 2-day gaps
    days, missing = placed_days(blocked)
    assert len(missing) == 1
    assert_order_and_recovery(days)


def test_category_windows_restrict_the_session() -> None:
    """B07: 'trainings only in the evening' is a hard rule; the preference cannot break it."""
    evenings = tuple(Interval(at(d, 18), at(d, 21)) for d in (1, 2, 3))
    placed = plan_activities([run(preferred_start=at(2, 8), allowed=evenings)], [], TZ).placed[0]
    assert any(w.start <= placed.start and placed.end <= w.end for w in evenings)


def test_category_windows_without_room_leave_the_session_unplaced() -> None:
    evenings = tuple(Interval(at(d, 18), at(d, 21)) for d in (1, 2, 3))
    busy = [Interval(at(d, 17), at(d, 22)) for d in (1, 2, 3)]
    plan = plan_activities([run(allowed=evenings)], busy, TZ)
    assert plan.unplaced == ["run"]


def test_empty_category_windows_mean_never() -> None:
    plan = plan_activities([run(allowed=())], [], TZ)
    assert plan.unplaced == ["run"]


def _sun(day: date) -> tuple[datetime, datetime]:
    """Winter daylight for the tests: 08:30-16:45 local."""
    return (
        datetime.combine(day, datetime.min.time(), TZ).replace(hour=8, minute=30),
        datetime.combine(day, datetime.min.time(), TZ).replace(hour=16, minute=45),
    )


def test_training_prefers_a_day_without_classes() -> None:
    from tempo.scheduler.activities import TrainingPreferences

    item = run(1, 2, preferred_start=at(1, 9), training=True)
    prefs = TrainingPreferences(free_days=True)
    placed = plan_activities(
        [item], [], TZ, training=prefs, class_days=frozenset({date(2026, 12, 1)})
    ).placed[0]
    assert placed.start.day == 2
    # Not a training (study, personal outing): unaffected.
    other = run(1, 2, preferred_start=at(1, 9))
    kept = plan_activities(
        [other], [], TZ, training=prefs, class_days=frozenset({date(2026, 12, 1)})
    ).placed[0]
    assert kept.start == at(1, 9)


def test_training_prefers_the_morning_then_the_afternoon() -> None:
    from tempo.scheduler.activities import TrainingPreferences

    item = run(2, 2, preferred_start=at(2, 15), training=True)
    placed = plan_activities(
        [item], [], TZ, training=TrainingPreferences(mornings=True), day_start=at(2, 8).time()
    ).placed[0]
    assert at(2, 8) <= placed.start and placed.end <= at(2, 12)
    busy = [Interval(at(2, 6), at(2, 12, 30))]  # morning taken: the afternoon is used
    later = plan_activities(
        [item], busy, TZ, training=TrainingPreferences(mornings=True), day_start=at(2, 8).time()
    ).placed[0]
    assert later.start >= at(2, 12, 30)


def test_training_avoids_the_dark_without_forbidding_it() -> None:
    from tempo.scheduler.activities import TrainingPreferences

    prefs = TrainingPreferences(daylight=True)
    item = run(2, 2, preferred_start=at(2, 18), training=True)
    placed = plan_activities([item], [], TZ, training=prefs, sun=_sun).placed[0]
    assert at(2, 8, 30) <= placed.start and placed.end <= at(2, 16, 45)
    busy = [Interval(at(2, 0), at(2, 17))]  # only the evening is free: still placed
    dark = plan_activities([item], busy, TZ, training=prefs, sun=_sun)
    assert dark.placed and dark.placed[0].start >= at(2, 17)
