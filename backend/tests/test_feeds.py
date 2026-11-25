"""Belgian feed registry and portal quota budget."""

from datetime import timedelta

import pytest

from tempo.transport.feeds import (
    FEEDS,
    AccessTier,
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
        assert item.url.startswith("https://api-management-discovery-production.azure-api.net/")
        assert item.url_for(AccessTier.ANONYMOUS) == item.url
    for item in REALTIME:
        assert item.url.endswith("?format=protobuf")


def test_keyed_tiers_use_the_authenticated_host_with_the_same_path() -> None:
    """Knowledge base, "Use with API key": api.belgianmobility.io checks the key (verified
    2026-11-25: 401 without key or with a wrong key); the anonymous host ignores any key."""
    anonymous = "https://api-management-discovery-production.azure-api.net/api/gtfs/feed/"
    keyed = "https://api.belgianmobility.io/api/gtfs/feed/"
    for item in FEEDS:
        url = item.url_for(AccessTier.STANDARD)
        assert url.startswith(keyed)
        assert url.removeprefix(keyed) == item.url.removeprefix(anonymous)


def test_only_tec_is_repaired() -> None:
    assert {f.operator for f in FEEDS if f.needs_repair} == {Operator.TEC}


def test_default_plan_for_all_realtime_feeds_fits_the_standard_quota() -> None:
    result = check_budget(REALTIME, PollingPlan(), AccessTier.STANDARD)
    # Trip updates every minute, alerts every 5 minutes: 3 x 1440 + 3 x 288 polls.
    assert result.requests_per_day == 3 * 1440 + 3 * 288
    assert result.allowed_per_day == 9_600
    # Headroom: well under half of the 12 000/day quota (static downloads, retries, changes).
    assert result.requests_per_day < 12_000 // 2
    assert result.requests_per_minute_peak <= 6


def test_all_realtime_feeds_do_not_fit_the_anonymous_quota() -> None:
    with pytest.raises(QuotaExceededError, match="anonymous"):
        check_budget(REALTIME, PollingPlan(), AccessTier.ANONYMOUS)


def test_anonymous_tier_allows_one_feed_every_thirty_minutes() -> None:
    plan = PollingPlan(trip_updates=timedelta(minutes=30))
    sncb = [feed(Operator.SNCB, FeedKind.TRIP_UPDATES)]
    assert check_budget(sncb, plan, AccessTier.ANONYMOUS).requests_per_day == 48


def test_polling_faster_than_the_publisher_is_refused() -> None:
    plan = PollingPlan(trip_updates=timedelta(seconds=10))
    with pytest.raises(ValueError, match="faster than the publisher"):
        budget(REALTIME, plan, AccessTier.STANDARD)


def test_static_feeds_count_one_request_per_day() -> None:
    static = [f for f in FEEDS if not f.is_realtime]
    assert budget(static, PollingPlan(), AccessTier.ANONYMOUS).requests_per_day == 4


def test_peak_minute_over_quota_is_refused() -> None:
    many = REALTIME * 2  # 12 simultaneous requests > 10/minute anonymous
    plan = PollingPlan(trip_updates=timedelta(days=1), service_alerts=timedelta(days=1))
    with pytest.raises(QuotaExceededError, match="minute"):
        check_budget(many, plan, AccessTier.ANONYMOUS)
