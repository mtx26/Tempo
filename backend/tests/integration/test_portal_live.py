"""Integration tests against the REAL Belgian Mobility gateway, with the user's key.

Opt-in only: set ``TEMPO_TEST_BMC_KEY`` (Standard key). Each run spends 3 requests of the
quota, so it is never run by default or in CI.

Assertions are invariants (decodable, fresh, TEC repaired), not counts: live content changes.
"""

import os
from datetime import timedelta

import pytest
from pydantic import SecretStr

from tempo.transport.feeds import FeedKind, Operator, PollingPlan, feed
from tempo.transport.gtfsrt import decode, summarize
from tempo.transport.rt_relay import FeedRelay

KEY = os.environ.get("TEMPO_TEST_BMC_KEY")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not KEY, reason="TEMPO_TEST_BMC_KEY not set: real gateway not queried"),
]
DAILY = PollingPlan(trip_updates=timedelta(days=1), service_alerts=timedelta(days=1))
SELECTED = (
    feed(Operator.TEC, FeedKind.TRIP_UPDATES),
    feed(Operator.SNCB, FeedKind.SERVICE_ALERTS),
    feed(Operator.DE_LIJN, FeedKind.TRIP_UPDATES),
)


@pytest.fixture(scope="module")
def relay() -> FeedRelay:
    return FeedRelay(SELECTED, DAILY, key=SecretStr(KEY or ""))


def test_tec_trip_updates_are_live_and_repaired(relay: FeedRelay) -> None:
    message = decode(relay.get(Operator.TEC, FeedKind.TRIP_UPDATES))
    assert message.entity
    for entity in message.entity:
        assert not entity.trip_update.trip.HasField("start_time")


def test_sncb_alerts_are_live(relay: FeedRelay) -> None:
    summary = summarize(decode(relay.get(Operator.SNCB, FeedKind.SERVICE_ALERTS)))
    assert summary.alerts == summary.entities


def test_de_lijn_trip_updates_are_live(relay: FeedRelay) -> None:
    summary = summarize(decode(relay.get(Operator.DE_LIJN, FeedKind.TRIP_UPDATES)))
    assert summary.trip_updates > 0
