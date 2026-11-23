"""University iCal import: expands a feed into FIXED occurrences and diffs successive syncs.

- Recurrences (RRULE/RDATE/EXDATE) and overridden occurrences (RECURRENCE-ID) are expanded.
- Cancelled events are dropped, so a later sync reports them as ``removed``.
- All-day events are reported in ``skipped``: they are not timed commitments and must not
  silently block a whole day.
- Floating (naive) times are interpreted in the configured default timezone; a TZID that cannot
  be resolved is rejected rather than reinterpreted (no fabricated times).

Known limitations (documented in docs/DECISIONS.md D012): moving the master DTSTART of a series,
or turning a single event into a series, changes occurrence keys, so the diff reports
removed + added instead of changed; floating times in a DST gap/overlap are not flagged.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import recurring_ical_events
from icalendar import Calendar

from tempo.domain.models import EventStatus, FixedEvent

type EventKey = tuple[str, str | None]
type Window = tuple[datetime, datetime]


MAX_UID_LENGTH = 512


class IcsParseError(ValueError):
    """The payload is not a usable iCalendar document."""


@dataclass(frozen=True, slots=True)
class IcsImport:
    events: list[FixedEvent]
    skipped: list[str] = field(default_factory=list)
    """Source items deliberately not imported, with the reason."""
    warnings: list[str] = field(default_factory=list)
    """Imported items whose source is suspicious and deserves user attention."""
    invalid_uids: set[str] = field(default_factory=set)
    """UIDs present in the source but unusable: stored occurrences must NOT be deleted for them."""


@dataclass(frozen=True, slots=True)
class EventDiff:
    added: list[FixedEvent]
    removed: list[FixedEvent]
    changed: list[tuple[FixedEvent, FixedEvent]]
    unchanged: int

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


@dataclass(slots=True)
class _SourceScan:
    recurring: set[str] = field(default_factory=set)
    invalid: dict[EventKey, str] = field(default_factory=dict)
    duplicates: dict[str, list[Any]] = field(default_factory=dict)


class _Skipped(list[str]):
    """Skipped-item messages plus the set of UIDs that were invalid in the source."""

    def __init__(self) -> None:
        super().__init__()
        self.invalid_uids: set[str] = set()
        self.dst: list[str] = []
        """Warnings about wall-clock times changed by a DST transition."""

    def invalid(self, uid: str, reason: object) -> None:
        self.invalid_uids.add(uid)
        self.append(f"{uid}: invalid event ({reason})")


def _as_aware(value: datetime, default_tz: ZoneInfo) -> datetime:
    return value.replace(tzinfo=default_tz) if value.tzinfo is None else value


def dst_normalise(value: datetime) -> tuple[datetime, str | None]:
    """Resolve wall-clock times that do not exist or exist twice because of a DST change.

    - Non-existent (spring-forward gap, e.g. 02:30 on the last Sunday of March in Brussels):
      the instant Python derives with ``fold=0`` is kept and the wall time is normalised to it
      (02:30 -> 03:30 CEST), never silently shifted.
    - Ambiguous (fall-back overlap, 02:30 happens twice): the FIRST occurrence (``fold=0``,
      summer time) is used — the earlier, conservative interpretation for a commitment.
    Both cases return a warning so the user can check the source.
    """
    if value.tzinfo is None:
        return value, None
    first = value.replace(fold=0)
    second = value.replace(fold=1)
    if first.utcoffset() == second.utcoffset():
        return first, None
    wall = first.replace(tzinfo=None)
    round_trip = first.astimezone(UTC).astimezone(value.tzinfo).replace(tzinfo=None)
    if round_trip != wall:
        normalised = first.astimezone(UTC).astimezone(value.tzinfo)
        return normalised, (
            f"{wall.isoformat(timespec='minutes')} does not exist (DST change): "
            f"interpreted as {normalised.isoformat(timespec='minutes')}"
        )
    return first, (
        f"{wall.isoformat(timespec='minutes')} is ambiguous (DST change): first occurrence "
        f"{first.isoformat(timespec='minutes')} used"
    )


def _utc_iso(value: datetime, default_tz: ZoneInfo) -> str:
    return _as_aware(value, default_tz).astimezone(UTC).isoformat()


def _text(component: Any, name: str) -> str | None:
    value = component.get(name)
    return None if value is None else str(value)


def _recurrence_key(component: Any, default_tz: ZoneInfo) -> str | None:
    recurrence = component.get("RECURRENCE-ID")
    if recurrence is None or not isinstance(recurrence.dt, datetime):
        return None
    return _utc_iso(recurrence.dt, default_tz)


def _source_problem(component: Any, default_tz: ZoneInfo) -> str | None:
    """Return why a raw VEVENT cannot be trusted, or ``None``."""
    if len(_text(component, "UID") or "") > MAX_UID_LENGTH:
        return f"UID longer than {MAX_UID_LENGTH} characters"
    values: dict[str, datetime] = {}
    for name in ("DTSTART", "DTEND"):
        if name not in component:
            continue
        decoded = component.decoded(name)
        tzid = component[name].params.get("TZID")
        if isinstance(decoded, datetime) and tzid and decoded.tzinfo is None:
            return f"unknown TZID {tzid!r}"
        if isinstance(decoded, datetime):
            values[name] = _as_aware(decoded, default_tz)
    if len(values) == 2 and values["DTEND"] <= values["DTSTART"]:
        return "DTEND is not after DTSTART in source"
    return None


def _scan_sources(calendar: Calendar, default_tz: ZoneInfo) -> _SourceScan:
    """Inspect raw VEVENTs before expansion.

    The expansion library adds a RECURRENCE-ID to *every* occurrence, silently swaps inverted
    DTSTART/DTEND, reinterprets unknown TZIDs and drops duplicate UIDs. Each is guarded here
    using the raw source components.
    """
    scan = _SourceScan()
    vevents = [c for c in calendar.walk("VEVENT") if _text(c, "UID")]
    masters: Counter[str] = Counter()
    for raw in vevents:
        uid = _text(raw, "UID") or ""
        if any(prop in raw for prop in ("RRULE", "RDATE", "RECURRENCE-ID")):
            scan.recurring.add(uid)
        if "RECURRENCE-ID" not in raw:
            masters[uid] += 1
    for raw in vevents:
        uid = _text(raw, "UID") or ""
        if masters[uid] > 1 and uid not in scan.recurring:
            scan.duplicates.setdefault(uid, []).append(raw)
            continue
        problem = _source_problem(raw, default_tz)
        if problem:
            scan.invalid[(uid, _recurrence_key(raw, default_tz))] = problem
    return scan


def _build_event(
    component: Any, uid: str, recurrence_id: str | None, default_tz: ZoneInfo, skipped: _Skipped
) -> FixedEvent | None:
    start = component.decoded("DTSTART")
    end = component.decoded("DTEND") if "DTEND" in component else None
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        kind = "all-day" if isinstance(start, date) else "untimed"
        skipped.append(f"{uid}: {kind} event not treated as a timed commitment")
        return None
    status_text = (_text(component, "STATUS") or "").upper()
    start_at, start_issue = dst_normalise(_as_aware(start, default_tz))
    end_at, end_issue = dst_normalise(_as_aware(end, default_tz))
    for issue in (start_issue, end_issue):
        if issue is not None:
            skipped.dst.append(f"{uid}: {issue}")
    try:
        return FixedEvent(
            uid=uid,
            recurrence_id=recurrence_id,
            start=start_at,
            end=end_at,
            summary=_text(component, "SUMMARY") or "",
            location=_text(component, "LOCATION"),
            status=EventStatus.TENTATIVE if status_text == "TENTATIVE" else EventStatus.CONFIRMED,
        )
    except ValueError as exc:
        skipped.invalid(uid, exc)
        return None


def _overlaps(event: FixedEvent, window: Window) -> bool:
    return event.start < window[1] and event.end > window[0]


def _import_duplicates(
    scan: _SourceScan, window: Window, default_tz: ZoneInfo, skipped: _Skipped
) -> tuple[list[FixedEvent], list[str]]:
    """Keep every event of a duplicated non-recurring UID, keyed by its start time."""
    events: list[FixedEvent] = []
    warnings: list[str] = []
    for uid, components in scan.duplicates.items():
        warnings.append(f"{uid}: duplicate UID in source ({len(components)} events); all kept")
        for raw in components:
            if (_text(raw, "STATUS") or "").upper() == "CANCELLED":
                continue
            problem = _source_problem(raw, default_tz)
            if problem:
                skipped.invalid(uid, problem)
                continue
            start = raw.decoded("DTSTART")
            key = _utc_iso(start, default_tz) if isinstance(start, datetime) else None
            event = _build_event(raw, uid, key, default_tz, skipped)
            if event is not None and _overlaps(event, window):
                events.append(event)
    return events, warnings


def parse_ics(
    data: bytes,
    window_start: datetime,
    window_end: datetime,
    *,
    default_tz: ZoneInfo,
) -> IcsImport:
    """Expand ``data`` into concrete occurrences overlapping ``[window_start, window_end)``."""
    try:
        calendar = Calendar.from_ical(data)
        scan = _scan_sources(calendar, default_tz)
        occurrences = recurring_ical_events.of(calendar).between(window_start, window_end)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise IcsParseError("invalid iCalendar payload") from exc

    skipped = _Skipped()
    for (invalid_uid, _), reason in scan.invalid.items():
        skipped.invalid(invalid_uid, reason)
    events, warnings = _import_duplicates(scan, (window_start, window_end), default_tz, skipped)
    for component in occurrences:
        if component.name != "VEVENT":
            continue
        uid = _text(component, "UID")
        if not uid:
            skipped.append("event without UID")
            continue
        if uid in scan.duplicates or (uid, None) in scan.invalid:
            continue
        if (_text(component, "STATUS") or "").upper() == "CANCELLED":
            continue
        recurrence_id = _recurrence_key(component, default_tz) if uid in scan.recurring else None
        if recurrence_id is not None and (uid, recurrence_id) in scan.invalid:
            continue
        event = _build_event(component, uid, recurrence_id, default_tz, skipped)
        if event is not None:
            events.append(event)
    events.sort(key=lambda e: (e.start, e.uid))
    return IcsImport(
        events=events,
        skipped=list(dict.fromkeys(skipped)),
        warnings=list(dict.fromkeys([*warnings, *skipped.dst])),
        invalid_uids=skipped.invalid_uids,
    )


def _index(events: Iterable[FixedEvent], window: Window | None) -> dict[EventKey, FixedEvent]:
    return {e.key: e for e in events if window is None or _overlaps(e, window)}


def diff_events(
    previous: Sequence[FixedEvent],
    current: Sequence[FixedEvent],
    *,
    window: Window | None = None,
) -> EventDiff:
    """Compare two syncs by stable occurrence key; identical feeds yield an empty diff.

    When the two syncs used different windows, pass their intersection as ``window`` so that
    occurrences which merely left the window are not reported as removed.
    """
    old, new = _index(previous, window), _index(current, window)
    common = old.keys() & new.keys()
    changed = [(old[k], new[k]) for k in common if old[k] != new[k]]
    return EventDiff(
        added=sorted((new[k] for k in new.keys() - old.keys()), key=lambda e: e.start),
        removed=sorted((old[k] for k in old.keys() - new.keys()), key=lambda e: e.start),
        changed=sorted(changed, key=lambda pair: pair[1].start),
        unchanged=sum(1 for k in common if old[k] == new[k]),
    )
