"""Flexible training placement (trainings move, keep duration; travel reserved)."""

import itertools
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tempo.scheduler.activities import FlexibleActivity, plan_activities
from tempo.scheduler.model import Interval

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
