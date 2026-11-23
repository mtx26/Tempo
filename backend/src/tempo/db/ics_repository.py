"""Persist iCal imports idempotently and safely.

- Occurrences are matched by (source, UID, RECURRENCE-ID); changes update rows in place.
- Disappeared occurrences are soft-deleted (``deleted_at``) to keep history; reappearing ones
  are restored. Occurrences whose UID is invalid in the current feed are never deleted.
- A feed that is empty, or would delete a large part of the stored window, is recorded as
  ``suspect`` and no deletion is applied (protects fixed classes from truncated downloads).
- Concurrent syncs of one source are serialised by a transaction-scoped advisory lock.
- Each sync writes a ``sync_run`` row and an audit entry listing occurrence keys and changed
  field names (no event titles or locations, to limit personal data in the audit log).

The caller owns the transaction. Callers should parse the feed over the sync window plus a
margin, so that an occurrence moved just beyond the window is seen as changed, not removed.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tempo.db.models import AuditEntry, CalendarSource, FixedEventRow, SyncRun
from tempo.domain.models import FixedEvent
from tempo.sync.ics import IcsImport, Window

type OccurrenceKey = tuple[str, str | None]

SUSPECT_MIN_REMOVALS = 3
SUSPECT_REMOVAL_RATIO = 0.5
_FIELDS = ("start_at", "end_at", "summary", "location", "status")


def get_or_create_source(session: Session, name: str) -> CalendarSource:
    session.execute(insert(CalendarSource).values(name=name).on_conflict_do_nothing())
    return session.scalars(select(CalendarSource).where(CalendarSource.name == name)).one()


def active_events(session: Session, source: CalendarSource) -> Sequence[FixedEventRow]:
    return session.scalars(
        select(FixedEventRow)
        .where(FixedEventRow.source_id == source.id, FixedEventRow.deleted_at.is_(None))
        .order_by(FixedEventRow.start_at)
    ).all()


def _changed_fields(row: FixedEventRow, event: FixedEvent) -> list[str]:
    incoming = {
        "start_at": event.start,
        "end_at": event.end,
        "summary": event.summary,
        "location": event.location,
        "status": event.status,
    }
    return [name for name in _FIELDS if getattr(row, name) != incoming[name]]


def _apply(row: FixedEventRow, event: FixedEvent, now: datetime) -> None:
    row.start_at, row.end_at = event.start, event.end
    row.summary, row.location, row.status = event.summary, event.location, event.status
    row.updated_at = now


def _find(session: Session, source: CalendarSource, key: OccurrenceKey) -> FixedEventRow | None:
    uid, recurrence_id = key
    condition = (
        FixedEventRow.recurrence_id.is_(None)
        if recurrence_id is None
        else FixedEventRow.recurrence_id == recurrence_id
    )
    return session.scalars(
        select(FixedEventRow).where(
            FixedEventRow.source_id == source.id, FixedEventRow.uid == uid, condition
        )
    ).first()


def _is_suspect(active_in_window: int, incoming_in_window: int, removals: int) -> bool:
    if active_in_window and incoming_in_window == 0:
        return True
    return removals >= SUSPECT_MIN_REMOVALS and removals > SUSPECT_REMOVAL_RATIO * active_in_window


class _ChangeLog:
    def __init__(self) -> None:
        self.counts = {"added": 0, "removed": 0, "changed": 0, "restored": 0}
        self.changes: list[dict[str, Any]] = []

    def record(self, change: str, key: OccurrenceKey, fields: list[str] | None = None) -> None:
        self.counts[change] += 1
        entry: dict[str, Any] = {"change": change, "uid": key[0], "recurrence_id": key[1]}
        if fields is not None:
            entry["fields"] = fields
        self.changes.append(entry)


def _upsert(
    session: Session,
    source: CalendarSource,
    stored: dict[OccurrenceKey, FixedEventRow],
    incoming: dict[OccurrenceKey, FixedEvent],
    log: _ChangeLog,
    now: datetime,
) -> None:
    for key, event in incoming.items():
        row = stored.get(key) or _find(session, source, key)
        if row is None:
            session.add(
                FixedEventRow(
                    source_id=source.id,
                    uid=event.uid,
                    recurrence_id=event.recurrence_id,
                    start_at=event.start,
                    end_at=event.end,
                    summary=event.summary,
                    location=event.location,
                    status=event.status,
                    first_seen_at=now,
                    updated_at=now,
                )
            )
            log.record("added", key)
        elif row.deleted_at is not None:
            _apply(row, event, now)
            row.deleted_at = None
            log.record("restored", key)
        elif fields := _changed_fields(row, event):
            _apply(row, event, now)
            log.record("changed", key, fields)


def _pair_moves(
    session: Session,
    source: CalendarSource,
    stored: dict[OccurrenceKey, FixedEventRow],
    incoming: dict[OccurrenceKey, FixedEvent],
) -> list[tuple[FixedEventRow, FixedEvent]]:
    """Occurrences whose key changed because the series moved (new RECURRENCE-ID) or a single
    event became recurring: pair vanished and new occurrences of the same UID so they are
    reported as CHANGED rows (D012), not removed + added.

    Same count for a UID: chronological one-to-one pairing (the whole series shifted).
    Otherwise: pairing only when an old and a new occurrence fall on the same local date."""
    vanished: dict[str, list[FixedEventRow]] = {}
    for key, row in stored.items():
        if key not in incoming and row.deleted_at is None:
            vanished.setdefault(key[0], []).append(row)
    appeared: dict[str, list[FixedEvent]] = {}
    for key, event in incoming.items():
        if key not in stored and _find(session, source, key) is None:
            appeared.setdefault(key[0], []).append(event)
    pairs: list[tuple[FixedEventRow, FixedEvent]] = []
    for uid in vanished.keys() & appeared.keys():
        old = sorted(vanished[uid], key=lambda r: r.start_at)
        new = sorted(appeared[uid], key=lambda e: e.start)
        if len(old) == len(new):
            pairs.extend(zip(old, new, strict=True))
            continue
        free = list(new)
        for row in old:
            day = row.start_at.astimezone(new[0].start.tzinfo).date()
            match = next((e for e in free if e.start.date() == day), None)
            if match is not None:
                free.remove(match)
                pairs.append((row, match))
    return pairs


def apply_ics_sync(
    session: Session,
    source: CalendarSource,
    imported: IcsImport,
    window: Window,
    *,
    now: datetime,
) -> SyncRun:
    """Reconcile stored occurrences overlapping ``window`` with ``imported``."""
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"ics:{source.id}"))))
    w_start, w_end = window
    rows = session.scalars(
        select(FixedEventRow).where(
            FixedEventRow.source_id == source.id,
            FixedEventRow.start_at < w_end,
            FixedEventRow.end_at > w_start,
        )
    ).all()
    stored: dict[OccurrenceKey, FixedEventRow] = {(r.uid, r.recurrence_id): r for r in rows}
    incoming = {event.key: event for event in imported.events}
    log = _ChangeLog()
    for row, event in _pair_moves(session, source, stored, incoming):
        fields = ["recurrence_id", *_changed_fields(row, event)]
        del stored[(row.uid, row.recurrence_id)]
        row.recurrence_id = event.recurrence_id
        _apply(row, event, now)
        stored[event.key] = row
        log.record("changed", event.key, sorted(set(fields)))
    _upsert(session, source, stored, incoming, log, now)

    to_remove = [
        (key, row)
        for key, row in stored.items()
        if key not in incoming and row.deleted_at is None and key[0] not in imported.invalid_uids
    ]
    active_in_window = sum(1 for row in stored.values() if row.deleted_at is None)
    incoming_in_window = sum(1 for e in imported.events if e.start < w_end and e.end > w_start)
    suspect = _is_suspect(active_in_window, incoming_in_window, len(to_remove))
    if not suspect:
        for key, row in to_remove:
            row.deleted_at = now
            row.updated_at = now
            log.record("removed", key)

    run = SyncRun(
        source_id=source.id,
        started_at=now,
        window_start=w_start,
        window_end=w_end,
        status="suspect" if suspect else "success",
        skipped={"skipped": imported.skipped, "warnings": imported.warnings},
        **log.counts,
    )
    session.add(run)
    session.flush()
    details: dict[str, Any] = {"source": source.name, **log.counts, "changes": log.changes}
    if suspect:
        details["withheld_removals"] = len(to_remove)
    session.add(
        AuditEntry(
            at=now,
            actor="worker",
            action="ics_sync",
            entity="sync_run",
            entity_id=str(run.id),
            details=details,
        )
    )
    session.flush()
    return run
