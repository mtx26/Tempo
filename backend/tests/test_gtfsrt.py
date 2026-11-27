"""GTFS-RT validation and TEC repair, on REAL feed extracts captured 2026-11-23 ~19:04 UTC.

Fixtures are a few entities of the Belgian Mobility feeds (CC BY 4.0: "Source: TEC / NMBS-SNCB /
De Lijn - Open Data - 2026-11-23").
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from google.transit import gtfs_realtime_pb2

from tempo.transport.gtfsrt import (
    InvalidFeedError,
    decode,
    drop_start_times,
    prepare,
    summarize,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gtfsrt"
NOW = datetime(2026, 11, 23, 19, 5, tzinfo=UTC)


def raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_real_tec_trip_updates_carry_afternoon_start_times_shifted_by_12_hours() -> None:
    message = decode(raw("tec_trip_updates.pbf"))
    trips = {e.trip_update.trip.trip_id: e.trip_update.trip.start_time for e in message.entity}
    # Static GTFS (tec.gtfs.zip of 2026-11-23): this trip departs at 20:05:00.
    assert trips["gt:tec:56324403-H_2026-H26_P2-Sem-N-3-11"] == "08:05:00"


def test_repair_drops_start_time_and_keeps_everything_else() -> None:
    original = decode(raw("tec_trip_updates.pbf"))
    result = prepare(raw("tec_trip_updates.pbf"), repair=True, now=NOW)
    repaired = decode(result.payload)
    assert result.start_times_removed == len(original.entity)
    assert len(repaired.entity) == len(original.entity)
    for before, after in zip(original.entity, repaired.entity, strict=True):
        assert not after.trip_update.trip.HasField("start_time")
        assert after.trip_update.trip.trip_id == before.trip_update.trip.trip_id
        assert after.trip_update.trip.start_date == before.trip_update.trip.start_date
        relationship = after.trip_update.trip.schedule_relationship
        assert relationship == before.trip_update.trip.schedule_relationship
        assert after.trip_update.stop_time_update == before.trip_update.stop_time_update


def test_repair_preserves_real_cancellations_and_delays() -> None:
    result = prepare(raw("tec_trip_updates.pbf"), repair=True, now=NOW)
    assert result.summary.cancelled_trips >= 1
    repaired = decode(result.payload)
    delays = [
        stu.departure.delay
        for e in repaired.entity
        for stu in e.trip_update.stop_time_update
        if stu.departure.delay
    ]
    assert delays


def test_repair_also_fixes_alert_trip_selectors() -> None:
    result = prepare(raw("tec_alerts.pbf"), repair=True, now=NOW)
    assert result.start_times_removed >= 1
    for entity in decode(result.payload).entity:
        for informed in entity.alert.informed_entity:
            assert not informed.trip.HasField("start_time")
    assert result.summary.alerts == 3


def test_feeds_without_repair_are_served_byte_for_byte() -> None:
    data = raw("delijn_trip_updates.pbf")
    result = prepare(data, repair=False, now=NOW)
    assert result.payload == data
    assert result.start_times_removed == 0
    assert result.summary.trip_updates == 5


def test_start_time_without_trip_id_is_kept() -> None:
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.timestamp = int(NOW.timestamp())
    trip = message.entity.add(id="1").trip_update.trip
    trip.route_id, trip.start_time, trip.start_date = "r", "08:00:00", "20261123"
    assert drop_start_times(message) == 0
    assert message.entity[0].trip_update.trip.start_time == "08:00:00"


def test_sncb_alerts_summary() -> None:
    summary = summarize(decode(raw("sncb_alerts.pbf")))
    assert summary.alerts == 3
    assert summary.produced_at == datetime(2026, 11, 23, 19, 4, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "payload",
    [b"", b"<!DOCTYPE HTML><html>quota</html>", b"\xff\xfe\x00garbage"],
)
def test_non_protobuf_payloads_are_rejected(payload: bytes) -> None:
    with pytest.raises(InvalidFeedError):
        prepare(payload, repair=False, now=NOW)


def test_message_without_version_is_rejected() -> None:
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.timestamp = int(NOW.timestamp())
    message.entity.add(id="1").alert.header_text.translation.add(text="x")
    with pytest.raises(InvalidFeedError):
        decode(message.SerializePartialToString())


def test_message_without_timestamp_is_rejected() -> None:
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    with pytest.raises(InvalidFeedError, match="timestamp"):
        decode(message.SerializeToString())


def test_partial_entity_and_differential_message_are_rejected() -> None:
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.timestamp = int(NOW.timestamp())
    message.entity.add(id="partial").trip_update.SetInParent()
    with pytest.raises(InvalidFeedError, match="required protobuf fields"):
        prepare(message.SerializePartialToString(), repair=True, now=NOW)
    message.ClearField("entity")
    message.header.incrementality = gtfs_realtime_pb2.FeedHeader.DIFFERENTIAL
    with pytest.raises(InvalidFeedError, match="differential"):
        prepare(message.SerializeToString(), repair=False, now=NOW)


def test_stale_feed_is_rejected() -> None:
    with pytest.raises(InvalidFeedError, match="stale"):
        prepare(raw("delijn_trip_updates.pbf"), repair=False, now=NOW + timedelta(minutes=15))


def test_feed_from_the_future_is_rejected() -> None:
    with pytest.raises(InvalidFeedError, match="future"):
        prepare(raw("delijn_trip_updates.pbf"), repair=False, now=NOW - timedelta(minutes=10))


def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="aware"):
        prepare(raw("delijn_trip_updates.pbf"), repair=False, now=NOW.replace(tzinfo=None))


def test_added_and_vehicle_descriptors_are_not_repaired() -> None:
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.timestamp = int(NOW.timestamp())
    added = message.entity.add(id="added").trip_update.trip
    added.trip_id, added.start_time = "extra-1", "20:05:00"
    added.schedule_relationship = gtfs_realtime_pb2.TripDescriptor.ADDED
    vehicle = message.entity.add(id="vp").vehicle.trip
    vehicle.trip_id, vehicle.start_time = "t-1", "08:05:00"
    cancelled = message.entity.add(id="c").trip_update.trip
    cancelled.trip_id, cancelled.start_time = "t-2", "08:05:00"
    cancelled.schedule_relationship = gtfs_realtime_pb2.TripDescriptor.CANCELED
    assert drop_start_times(message) == 1
    assert message.entity[0].trip_update.trip.start_time == "20:05:00"
    assert message.entity[1].vehicle.trip.start_time == "08:05:00"
    assert not message.entity[2].trip_update.trip.HasField("start_time")
