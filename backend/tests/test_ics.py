from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tempo.domain.models import FixedEvent
from tempo.sync.ics import IcsParseError, diff_events, parse_ics

BRUSSELS = ZoneInfo("Europe/Brussels")
FIXTURE = (Path(__file__).parent / "fixtures" / "university.ics").read_bytes()
WINDOW = (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC))


def parse(data: bytes = FIXTURE) -> list[FixedEvent]:
    return parse_ics(data, *WINDOW, default_tz=BRUSSELS).events


def by_summary(events: list[FixedEvent], text: str) -> list[FixedEvent]:
    return sorted((e for e in events if text in e.summary), key=lambda e: e.start)


def test_weekly_class_expands_without_excluded_date() -> None:
    starts = [e.start.astimezone(BRUSSELS) for e in by_summary(parse(), "Analyse")]
    assert [s.date().isoformat() for s in starts] == ["2026-10-19", "2026-10-26", "2026-11-09"]


def test_local_wall_clock_is_kept_across_dst_change() -> None:
    first, _, last = by_summary(parse(), "Analyse")
    assert first.start.astimezone(BRUSSELS).hour == 8
    assert last.start.astimezone(BRUSSELS).hour == 8
    assert first.start.astimezone(UTC).hour == 6  # CEST
    assert last.start.astimezone(UTC).hour == 7  # CET


def test_overridden_occurrence_uses_override_and_keeps_identity() -> None:
    moved = by_summary(parse(), "local change")
    assert len(moved) == 1
    assert moved[0].location == "Auditoire B2"
    assert moved[0].start.astimezone(BRUSSELS).hour == 10
    assert moved[0].uid == "analyse-weekly@univ.test"
    assert moved[0].recurrence_id is not None


def test_cancelled_events_are_not_imported() -> None:
    assert by_summary(parse(), "Physique") == []


def test_all_day_events_are_reported_as_skipped_not_blocking() -> None:
    result = parse_ics(FIXTURE, *WINDOW, default_tz=BRUSSELS)
    assert by_summary(result.events, "Conge") == []
    assert any("holiday@univ.test" in s for s in result.skipped)


def test_floating_times_use_default_timezone() -> None:
    (labo,) = by_summary(parse(), "Labo")
    assert labo.start.tzinfo is not None
    assert labo.start.astimezone(BRUSSELS).hour == 13


def test_occurrence_keys_are_unique() -> None:
    events = parse()
    assert len({e.key for e in events}) == len(events)


def test_invalid_calendar_raises_parse_error() -> None:
    with pytest.raises(IcsParseError):
        parse_ics(b"not a calendar", *WINDOW, default_tz=BRUSSELS)


def test_resync_of_identical_feed_is_a_no_op() -> None:
    diff = diff_events(parse(), parse())
    assert diff.is_empty
    assert diff.unchanged == len(parse())


def test_diff_detects_moved_added_and_removed_occurrences() -> None:
    before = parse()
    target = by_summary(before, "Labo")[0]
    moved = FixedEvent(
        uid=target.uid,
        recurrence_id=target.recurrence_id,
        start=target.start + timedelta(hours=1),
        end=target.end + timedelta(hours=1),
        summary=target.summary,
    )
    extra = FixedEvent(uid="new@univ.test", start=target.start, end=target.end, summary="Nouveau")
    after = [e for e in before if e.key != target.key and "local change" not in e.summary]
    after += [moved, extra]

    diff = diff_events(before, after)
    assert [new.key for _, new in diff.changed] == [target.key]
    assert [e.uid for e in diff.added] == ["new@univ.test"]
    assert [e.summary for e in diff.removed] == ["Analyse - Cours (local change)"]


def _calendar(*lines: str) -> bytes:
    body = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Tempo tests//EN", *lines, "END:VCALENDAR"]
    return "\r\n".join(body).encode()


def test_event_without_uid_is_skipped_not_imported() -> None:
    data = _calendar(
        "BEGIN:VEVENT",
        "DTSTAMP:20260901T000000Z",
        "SUMMARY:Sans UID",
        "DTSTART:20261022T130000Z",
        "DTEND:20261022T140000Z",
        "END:VEVENT",
    )
    result = parse_ics(data, *WINDOW, default_tz=BRUSSELS)
    assert result.events == []
    assert result.skipped == ["event without UID"]


def test_event_ending_before_start_is_skipped_with_reason() -> None:
    data = _calendar(
        "BEGIN:VEVENT",
        "UID:broken@univ.test",
        "DTSTAMP:20260901T000000Z",
        "SUMMARY:Incoherent",
        "DTSTART:20261022T140000Z",
        "DTEND:20261022T130000Z",
        "END:VEVENT",
    )
    result = parse_ics(data, *WINDOW, default_tz=BRUSSELS)
    assert result.events == []
    assert result.skipped and result.skipped[0].startswith("broken@univ.test: invalid event")


def _single(start: str, end: str) -> bytes:
    return _calendar(
        "BEGIN:VEVENT",
        "UID:single@univ.test",
        "DTSTAMP:20260901T000000Z",
        "SUMMARY:Examen blanc",
        f"DTSTART:{start}",
        f"DTEND:{end}",
        "END:VEVENT",
    )


def test_non_recurring_event_moved_in_source_is_reported_as_changed() -> None:
    before = parse(_single("20261023T140000Z", "20261023T150000Z"))
    after = parse(_single("20261023T160000Z", "20261023T170000Z"))
    diff = diff_events(before, after)
    assert diff.added == [] and diff.removed == []
    assert len(diff.changed) == 1
    assert before[0].recurrence_id is None


def _event(uid: str, *props: str) -> list[str]:
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        "DTSTAMP:20260901T000000Z",
        "SUMMARY:Cours",
        *props,
        "END:VEVENT",
    ]


def test_mixed_floating_and_utc_times_do_not_crash() -> None:
    data = _calendar(*_event("mixed", "DTSTART:20261022T130000", "DTEND:20261022T140000Z"))
    result = parse_ics(data, *WINDOW, default_tz=BRUSSELS)
    assert [e.uid for e in result.events] == ["mixed"]


def test_invalid_override_only_drops_that_occurrence() -> None:
    data = _calendar(
        *_event(
            "series",
            "DTSTART:20261019T080000Z",
            "DTEND:20261019T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=3",
        ),
        *_event(
            "series",
            "RECURRENCE-ID:20261026T080000Z",
            "DTSTART:20261026T100000Z",
            "DTEND:20261026T090000Z",
        ),
    )
    result = parse_ics(data, *WINDOW, default_tz=BRUSSELS)
    assert [e.start.date().isoformat() for e in result.events] == ["2026-10-19", "2026-11-02"]
    assert any("series" in s and "invalid" in s for s in result.skipped)


def test_duplicate_uids_keep_every_event_and_warn() -> None:
    data = _calendar(
        *_event("dup", "DTSTART:20261022T080000Z", "DTEND:20261022T090000Z"),
        *_event("dup", "DTSTART:20261023T080000Z", "DTEND:20261023T090000Z"),
    )
    result = parse_ics(data, *WINDOW, default_tz=BRUSSELS)
    assert [e.start.day for e in result.events] == [22, 23]
    assert len({e.key for e in result.events}) == 2
    assert any("dup" in w and "duplicate" in w for w in result.warnings)


def test_unknown_tzid_is_not_silently_reinterpreted() -> None:
    data = _calendar(
        *_event(
            "weird-tz", "DTSTART;TZID=Foo/Bar:20261022T080000", "DTEND;TZID=Foo/Bar:20261022T090000"
        )
    )
    result = parse_ics(data, *WINDOW, default_tz=BRUSSELS)
    assert result.events == []
    assert any("weird-tz" in s and "TZID" in s for s in result.skipped)


def test_recurring_all_day_event_is_reported_once() -> None:
    data = _calendar(
        *_event(
            "blocus",
            "DTSTART;VALUE=DATE:20261102",
            "DTEND;VALUE=DATE:20261103",
            "RRULE:FREQ=DAILY;COUNT=5",
        )
    )
    result = parse_ics(data, *WINDOW, default_tz=BRUSSELS)
    assert len(result.skipped) == 1


def test_moving_sync_window_does_not_report_false_removals() -> None:
    data = _calendar(
        *_event(
            "weekly",
            "DTSTART:20261005T080000Z",
            "DTEND:20261005T100000Z",
            "RRULE:FREQ=WEEKLY;UNTIL=20261207T235959Z",
        )
    )
    old_window = (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC))
    new_window = (datetime(2026, 10, 20, tzinfo=UTC), datetime(2027, 1, 31, tzinfo=UTC))
    before = parse_ics(data, *old_window, default_tz=BRUSSELS).events
    after = parse_ics(data, *new_window, default_tz=BRUSSELS).events
    diff = diff_events(before, after, window=(new_window[0], old_window[1]))
    assert diff.is_empty
