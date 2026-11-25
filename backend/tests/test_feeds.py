"""Belgian feed registry and portal quota budget."""

from datetime import timedelta

import pytest

from tempo.transport.feeds import (
    FEEDS,
    FeedKind,
    Operator,
    PollingPlan,
    QuotaExceededError,
    budget,
    check_budget,
    feed,
)

REALTIME = [f for f in FEEDS if f.is_realtime]


def test_registry_holds_the_ten_requested_feeds() -> None:
    assert len(FEEDS) == 10
    kinds = {(f.operator, f.kind) for f in FEEDS}
    assert len(kinds) == 10
    for operator in Operator:
        assert (operator, FeedKind.STATIC) in kinds
    for operator in (Operator.TEC, Operator.SNCB, Operator.DE_LIJN):
        assert (operator, FeedKind.TRIP_UPDATES) in kinds
        assert (operator, FeedKind.SERVICE_ALERTS) in kinds


def test_stib_has_no_realtime_feed() -> None:
    with pytest.raises(KeyError):
        feed(Operator.STIB, FeedKind.TRIP_UPDATES)
    with pytest.raises(KeyError):
        feed(Operator.STIB, FeedKind.SERVICE_ALERTS)


def test_urls_are_https_and_realtime_requests_protobuf() -> None:
    for item in FEEDS:
        assert item.url.startswith(
            "https://api-management-opendata-production.azure-api.net/api/gtfs/feed/"
        )
    for item in REALTIME:
        assert "format=protobuf" in item.url.split("?")[1]
    for item in REALTIME:
        assert ("unroll=off" in item.url) is (item.kind is FeedKind.TRIP_UPDATES)


def test_only_tec_is_repaired() -> None:
    assert {f.operator for f in FEEDS if f.needs_repair} == {Operator.TEC}


def test_default_plan_for_all_realtime_feeds_fits_the_standard_quota() -> None:
    result = check_budget(REALTIME, PollingPlan())
    # Trip updates every minute, alerts every 5 minutes: 3 x 1440 + 3 x 288 polls.
    assert result.requests_per_day == 3 * 1440 + 3 * 288
    assert result.allowed_per_day == 9_600
    # Headroom: well under half of the 12 000/day quota (static downloads, retries, changes).
    assert result.requests_per_day < 12_000 // 2
    assert result.requests_per_minute_peak <= 6


def test_polling_faster_than_the_publisher_is_refused() -> None:
    plan = PollingPlan(trip_updates=timedelta(seconds=10))
    with pytest.raises(ValueError, match="faster than the publisher"):
        budget(REALTIME, plan)


def test_static_feeds_count_one_request_per_day() -> None:
    static = [f for f in FEEDS if not f.is_realtime]
    assert budget(static, PollingPlan()).requests_per_day == 4


def test_peak_minute_over_quota_is_refused() -> None:
    many = REALTIME * 84  # 504 simultaneous requests > 500/minute
    plan = PollingPlan(trip_updates=timedelta(days=1), service_alerts=timedelta(days=1))
    with pytest.raises(QuotaExceededError, match="minute"):
        check_budget(many, plan)


def test_more_than_the_daily_quota_is_refused() -> None:
    plan = PollingPlan(trip_updates=timedelta(seconds=30), service_alerts=timedelta(seconds=30))
    with pytest.raises(QuotaExceededError, match="requests/day"):
        check_budget(REALTIME, plan)  # 6 x 2880 = 17 280 > 9 600
