"""GTFS-Realtime decoding, validation and repair of Belgian feeds before MOTIS consumes them.

Repair (only for feeds flagged ``needs_repair`` in ``feeds.FEEDS``, i.e. TEC):
TEC publishes ``trip.start_time`` shifted by 12 h for afternoon trips, so MOTIS cannot match
about half of the trip updates and a quarter of the alert selectors (measured 2026-11-23).
The GTFS-RT reference says that for trips not defined in ``frequencies.txt`` ``start_time``
"should either be omitted or be equal to the value in the GTFS feed". TEC's static feed has no
``frequencies.txt`` (checked 2026-11-23, re-checked by ``gtfs_update`` on every
download), so dropping ``start_time`` of SCHEDULED/CANCELED trips identified by ``trip_id`` is
spec-compliant and lets MOTIS match on ``trip_id`` + ``start_date`` (100 % resolved in a live
MOTIS test). Nothing else is altered; delays and cancellations are kept as published.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

MAX_FEED_AGE = timedelta(minutes=10)
"""Age limit for service alerts and generic validator calls."""
TRIP_UPDATE_MAX_AGE = timedelta(seconds=90)
"""GTFS Realtime best practices, for feeds published every 30 seconds."""
MAX_CLOCK_SKEW = timedelta(minutes=2)


class InvalidFeedError(ValueError):
    """Bytes are not a usable GTFS-RT FeedMessage (HTML error page, truncated, stale...)."""


class StaleFeedError(InvalidFeedError):
    """A valid message whose timestamp is too old."""

    def __init__(self, produced_at: datetime, age: timedelta) -> None:
        super().__init__(f"stale feed: produced {int(age.total_seconds())} s ago")
        self.produced_at = produced_at


@dataclass(frozen=True, slots=True)
class FeedSummary:
    produced_at: datetime
    entities: int
    trip_updates: int
    cancelled_trips: int
    alerts: int


@dataclass(frozen=True, slots=True)
class RepairResult:
    payload: bytes
    summary: FeedSummary
    start_times_removed: int


def decode(raw: bytes) -> gtfs_realtime_pb2.FeedMessage:
    if not raw:
        raise InvalidFeedError("empty feed")
    message = gtfs_realtime_pb2.FeedMessage()
    try:
        message.ParseFromString(raw)
    except DecodeError as exc:
        raise InvalidFeedError("not a GTFS-RT protobuf message") from exc
    if not message.header.gtfs_realtime_version:
        raise InvalidFeedError("missing gtfs_realtime_version")
    if not message.header.timestamp:
        raise InvalidFeedError("missing header timestamp")
    if not message.IsInitialized():
        raise InvalidFeedError("missing required protobuf fields")
    if message.header.incrementality == gtfs_realtime_pb2.FeedHeader.DIFFERENTIAL:
        raise InvalidFeedError("differential feed cannot replace a complete snapshot")
    return message


def summarize(message: gtfs_realtime_pb2.FeedMessage) -> FeedSummary:
    trip_updates = cancelled = alerts = 0
    canceled = gtfs_realtime_pb2.TripDescriptor.CANCELED
    for entity in message.entity:
        if entity.HasField("trip_update"):
            trip_updates += 1
            cancelled += entity.trip_update.trip.schedule_relationship == canceled
        if entity.HasField("alert"):
            alerts += 1
    try:
        produced_at = datetime.fromtimestamp(message.header.timestamp, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidFeedError("invalid header timestamp") from exc
    return FeedSummary(
        produced_at=produced_at,
        entities=len(message.entity),
        trip_updates=trip_updates,
        cancelled_trips=cancelled,
        alerts=alerts,
    )


def check_fresh(summary: FeedSummary, now: datetime, max_age: timedelta = MAX_FEED_AGE) -> None:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    age = now.astimezone(UTC) - summary.produced_at
    if age > max_age:
        raise StaleFeedError(summary.produced_at, age)
    if age < -MAX_CLOCK_SKEW:
        raise InvalidFeedError("feed timestamp is in the future")


REPAIRABLE = frozenset(
    {
        gtfs_realtime_pb2.TripDescriptor.SCHEDULED,
        gtfs_realtime_pb2.TripDescriptor.CANCELED,
    }
)
"""Only trips that exist in the static timetable are matched by ``trip_id``; ADDED/NEW/
DUPLICATED/... trips need their ``start_time`` and are left untouched."""


def _trip_descriptors(
    message: gtfs_realtime_pb2.FeedMessage,
) -> list[gtfs_realtime_pb2.TripDescriptor]:
    """Trip descriptors of trip updates and alert selectors (vehicle positions not relayed)."""
    found: list[gtfs_realtime_pb2.TripDescriptor] = []
    for entity in message.entity:
        if entity.HasField("trip_update"):
            found.append(entity.trip_update.trip)
        if entity.HasField("alert"):
            found.extend(ie.trip for ie in entity.alert.informed_entity if ie.HasField("trip"))
    return found


def drop_start_times(message: gtfs_realtime_pb2.FeedMessage) -> int:
    """Remove ``start_time`` from SCHEDULED/CANCELED trip descriptors identified by ``trip_id``
    (in place). Valid only for feeds whose static GTFS has no ``frequencies.txt``."""
    removed = 0
    for descriptor in _trip_descriptors(message):
        if (
            descriptor.trip_id
            and descriptor.HasField("start_time")
            and descriptor.schedule_relationship in REPAIRABLE
        ):
            descriptor.ClearField("start_time")
            removed += 1
    return removed


def prepare(
    raw: bytes, *, repair: bool, now: datetime, max_age: timedelta = MAX_FEED_AGE
) -> RepairResult:
    """Validate a downloaded feed (decodable, fresh) and repair it when required."""
    message = decode(raw)
    summary = summarize(message)
    check_fresh(summary, now, max_age)
    if not repair:
        return RepairResult(raw, summary, 0)
    removed = drop_start_times(message)
    return RepairResult(message.SerializeToString(), summary, removed)
