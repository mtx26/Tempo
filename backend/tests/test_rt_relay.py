"""GTFS-RT relay: quota enforcement, back-off, freshness, key handling (mocked portal)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tempo.transport.feeds import (
    FEEDS,
    KEY_HEADER,
    FeedKind,
    MissingKeyError,
    Operator,
    PollingPlan,
    QuotaExceededError,
    feed,
)
from tempo.transport.gtfsrt import decode
from tempo.transport.rt_relay import (
    FeedRelay,
    RelaySettings,
    RelayUnavailableError,
    create_relay_app,
    parse_feeds,
    relay_from_settings,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gtfsrt"
TEC_TU = (FIXTURES / "tec_trip_updates.pbf").read_bytes()
DELIJN_TU = (FIXTURES / "delijn_trip_updates.pbf").read_bytes()
KEY = SecretStr("k" * 32)
REALTIME = tuple(f for f in FEEDS if f.is_realtime)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 23, 19, 5, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Portal:
    """Fake Belgian Mobility portal recording requests."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.status = 200
        self.headers: dict[str, str] = {}
        self.body_by_operator = {"tec": TEC_TU, "delijn": DELIJN_TU, "nmbssncb": DELIJN_TU}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        operator = request.url.path.split("/")[4]
        if self.status != 200:
            return httpx2.Response(self.status, headers=self.headers, text="<html>no</html>")
        return httpx2.Response(200, content=self.body_by_operator[operator])


def make_relay(
    portal: Portal,
    clock: Clock,
    *,
    feeds: tuple = REALTIME,  # type: ignore[type-arg]
    plan: PollingPlan | None = None,
) -> FeedRelay:
    return FeedRelay(
        feeds,
        plan or PollingPlan(),
        key=KEY,
        http_client=httpx2.Client(transport=httpx2.MockTransport(portal)),
        clock=clock,
    )


def test_health_distinguishes_invalid_degraded_and_stale_feed() -> None:
    portal, clock = Portal(), Clock()
    relay = make_relay(portal, clock)
    key = feed(Operator.TEC, FeedKind.TRIP_UPDATES).name

    def status() -> dict[str, object]:
        values = relay.status()["feeds"]
        assert isinstance(values, dict)
        return dict(values[key])

    assert status()["state"] == "unavailable"
    portal.body_by_operator["tec"] = b"<html>"
    with pytest.raises(RelayUnavailableError):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    assert status()["state"] == "invalid"
    assert status()["consecutive_errors"] == 1

    relay = make_relay(portal, clock)
    portal.body_by_operator["tec"] = _restamp(TEC_TU, clock.now)
    relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    snapshot = status()
    assert snapshot["state"] == "operational"
    assert snapshot["last_success_at"] == clock.now.isoformat()
    assert isinstance(snapshot["response_ms"], float)
    portal.status = 503
    clock.now += timedelta(seconds=61)
    relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    assert status()["state"] == "degraded"
    assert status()["last_success_at"] == snapshot["last_success_at"]
    clock.now += timedelta(minutes=12)
    with pytest.raises(RelayUnavailableError, match="stale"):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    assert status()["state"] == "stale"


def test_copy_that_expires_during_request_is_never_served() -> None:
    portal, clock = Portal(), Clock()
    produced = clock.now
    portal.body_by_operator["tec"] = _restamp(TEC_TU, produced)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if portal.status == 503:
            clock.now += timedelta(seconds=2)
        return portal(request)

    relay = FeedRelay(
        REALTIME,
        PollingPlan(),
        key=KEY,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        clock=clock,
    )
    relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    clock.now = produced + timedelta(seconds=89)
    portal.status = 503
    with pytest.raises(RelayUnavailableError, match="stale"):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)


def test_tec_is_served_repaired_with_the_key_header() -> None:
    portal, clock = Portal(), Clock()
    portal.body_by_operator["tec"] = _restamp(TEC_TU, clock.now)
    relay = make_relay(portal, clock)
    payload = relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    entities = decode(payload).entity
    assert entities and all(not e.trip_update.trip.HasField("start_time") for e in entities)
    request = portal.requests[0]
    assert request.headers[KEY_HEADER] == KEY.get_secret_value()
    assert str(request.url) == feed(Operator.TEC, FeedKind.TRIP_UPDATES).url
    assert request.url.host == "api-management-opendata-production.azure-api.net"


def test_cached_within_interval_and_refetched_after() -> None:
    portal, clock = Portal(), Clock()
    portal.body_by_operator["delijn"] = _restamp(DELIJN_TU, clock.now)
    relay = make_relay(portal, clock)
    relay.get(Operator.DE_LIJN, FeedKind.TRIP_UPDATES)
    clock.now += timedelta(seconds=30)
    portal.body_by_operator["delijn"] = _restamp(DELIJN_TU, clock.now)
    relay.get(Operator.DE_LIJN, FeedKind.TRIP_UPDATES)
    assert len(portal.requests) == 1
    clock.now += timedelta(seconds=30)
    relay.get(Operator.DE_LIJN, FeedKind.TRIP_UPDATES)
    assert len(portal.requests) == 2
    assert relay.requests_last_24h == 2


def test_429_without_cache_is_unavailable_and_backs_off() -> None:
    portal, clock = Portal(), Clock()
    portal.status, portal.headers = 429, {"Retry-After": "600"}
    relay = make_relay(portal, clock)
    with pytest.raises(RelayUnavailableError, match="429"):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    clock.now += timedelta(minutes=5)
    with pytest.raises(RelayUnavailableError):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    assert len(portal.requests) == 1  # no retry before Retry-After
    portal.status = 200
    clock.now += timedelta(minutes=6)
    portal.body_by_operator["tec"] = _restamp(TEC_TU, clock.now)
    assert relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    assert len(portal.requests) == 2


def test_upstream_error_keeps_serving_a_still_fresh_copy() -> None:
    portal, clock = Portal(), Clock()
    portal.body_by_operator["delijn"] = _restamp(DELIJN_TU, clock.now)
    relay = make_relay(portal, clock)
    first = relay.get(Operator.DE_LIJN, FeedKind.TRIP_UPDATES)
    portal.status = 500
    clock.now += timedelta(seconds=61)
    assert relay.get(Operator.DE_LIJN, FeedKind.TRIP_UPDATES) == first


def test_old_trip_updates_are_expired_while_alerts_are_still_usable() -> None:
    portal, clock = Portal(), Clock()
    portal.body_by_operator["tec"] = _restamp(TEC_TU, clock.now - timedelta(seconds=91))
    portal.body_by_operator["nmbssncb"] = _restamp(
        (FIXTURES / "sncb_alerts.pbf").read_bytes(), clock.now - timedelta(seconds=91)
    )
    client = TestClient(create_relay_app(make_relay(portal, clock)))
    assert client.get("/feeds/tec/trip-updates.pbf").status_code == 503
    assert client.get("/feeds/nmbssncb/alerts.pbf").status_code == 200
    health = client.get("/health").json()["feeds"]
    assert health["tec/gtfs-rt-trip-updates"]["state"] == "stale"
    assert health["tec/gtfs-rt-trip-updates"]["age_seconds"] == 91


def test_recent_header_does_not_refresh_old_or_undated_predictions() -> None:
    portal, clock = Portal(), Clock()
    payload = decode(_restamp(DELIJN_TU, clock.now))
    payload.entity[0].trip_update.timestamp -= 91
    payload.entity[1].trip_update.ClearField("timestamp")
    payload.entity[2].trip_update.timestamp -= 80
    portal.body_by_operator["delijn"] = payload.SerializeToString()
    relay = make_relay(portal, clock)
    ids = {
        entity.id for entity in decode(relay.get(Operator.DE_LIJN, FeedKind.TRIP_UPDATES)).entity
    }
    assert ids == {entity.id for entity in payload.entity[2:]}
    clock.now += timedelta(seconds=11)
    ids = {
        entity.id for entity in decode(relay.get(Operator.DE_LIJN, FeedKind.TRIP_UPDATES)).entity
    }
    assert ids == {entity.id for entity in payload.entity[3:]}
    assert len(portal.requests) == 1
    health = TestClient(create_relay_app(relay)).get("/health").json()
    feed = health["feeds"]["delijn/gtfs-rt-trip-updates"]
    assert feed["state"] == "degraded"
    assert feed["entity_freshness"] == {"fresh": 2, "stale": 2, "missing_timestamp": 1, "future": 0}


def test_stale_copy_is_never_served() -> None:
    portal, clock = Portal(), Clock()
    relay = make_relay(portal, clock)
    relay.get(Operator.DE_LIJN, FeedKind.TRIP_UPDATES)
    portal.status = 500
    clock.now += timedelta(minutes=20)
    with pytest.raises(RelayUnavailableError, match="stale"):
        relay.get(Operator.DE_LIJN, FeedKind.TRIP_UPDATES)


def test_html_error_page_with_200_is_not_served() -> None:
    portal, clock = Portal(), Clock()
    portal.body_by_operator["tec"] = b"<!DOCTYPE HTML><html>maintenance</html>"
    relay = make_relay(portal, clock)
    with pytest.raises(RelayUnavailableError, match="protobuf"):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)


def test_network_error_is_unavailable() -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    relay = FeedRelay(
        REALTIME,
        PollingPlan(),
        key=KEY,
        http_client=httpx2.Client(transport=httpx2.MockTransport(broken)),
        clock=Clock(),
    )
    with pytest.raises(RelayUnavailableError, match="ConnectError"):
        relay.get(Operator.SNCB, FeedKind.SERVICE_ALERTS)


def test_rolling_budget_is_enforced_and_persisted(tmp_path: Path) -> None:
    portal, clock = Portal(), Clock()
    state = tmp_path / "relay-state.json"
    full = [(clock.now - timedelta(hours=23, minutes=59 - i % 50)).isoformat() for i in range(9600)]
    state.write_text(json.dumps({"requests": full}), encoding="utf-8")
    sncb = (feed(Operator.SNCB, FeedKind.TRIP_UPDATES),)
    plan = PollingPlan(trip_updates=timedelta(minutes=30))
    relay = FeedRelay(
        sncb,
        plan,
        key=KEY,
        http_client=httpx2.Client(transport=httpx2.MockTransport(portal)),
        clock=clock,
        state_file=state,
    )
    assert relay.requests_last_24h == 9600
    with pytest.raises(RelayUnavailableError, match="budget"):
        relay.get(Operator.SNCB, FeedKind.TRIP_UPDATES)
    assert not portal.requests
    clock.now += timedelta(hours=1)  # the oldest requests leave the 24 h window
    portal.body_by_operator["nmbssncb"] = _restamp(DELIJN_TU, clock.now)
    assert relay.get(Operator.SNCB, FeedKind.TRIP_UPDATES)
    saved = json.loads(state.read_text(encoding="utf-8"))["requests"]
    assert saved[-1] == clock.now.isoformat()
    assert len(saved) == relay.requests_last_24h < 80


def test_restart_keeps_the_request_log(tmp_path: Path) -> None:
    portal, clock = Portal(), Clock()
    state = tmp_path / "relay-state.json"

    def build() -> FeedRelay:
        return FeedRelay(
            REALTIME,
            PollingPlan(),
            key=KEY,
            http_client=httpx2.Client(transport=httpx2.MockTransport(portal)),
            clock=clock,
            state_file=state,
        )

    build().get(Operator.TEC, FeedKind.TRIP_UPDATES)
    assert build().requests_last_24h == 1


def test_corrupt_state_file_refuses_to_start(tmp_path: Path) -> None:
    state = tmp_path / "relay-state.json"
    for content in ("not json", '{"requests": ["2026-11-23T10:00:00"]}', '{"x": 1}'):
        state.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match="state file"):
            FeedRelay(REALTIME, PollingPlan(), key=KEY, state_file=state)


def test_retry_after_zero_still_backs_off_and_long_retry_after_is_honoured() -> None:
    portal, clock = Portal(), Clock()
    portal.status, portal.headers = 429, {"Retry-After": "0"}
    relay = make_relay(portal, clock)
    with pytest.raises(RelayUnavailableError):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    clock.now += timedelta(minutes=4)
    with pytest.raises(RelayUnavailableError):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    assert len(portal.requests) == 1
    portal.headers = {"Retry-After": "7200"}
    clock.now += timedelta(minutes=2)
    with pytest.raises(RelayUnavailableError):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    assert len(portal.requests) == 2
    clock.now += timedelta(hours=1, minutes=30)  # beyond MAX_BACKOFF, before Retry-After
    with pytest.raises(RelayUnavailableError):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    assert len(portal.requests) == 2


def test_budget_exhaustion_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    portal, clock = Portal(), Clock()
    sncb = (feed(Operator.SNCB, FeedKind.TRIP_UPDATES),)
    plan = PollingPlan(trip_updates=timedelta(minutes=30))
    relay = make_relay(portal, clock, feeds=sncb, plan=plan)
    relay._sent.extend([clock.now] * 9600)
    for _ in range(3):
        with pytest.raises(RelayUnavailableError):
            relay.get(Operator.SNCB, FeedKind.TRIP_UPDATES)
    assert sum("budget exhausted" in r.message for r in caplog.records) == 1


def test_empty_or_blank_key_is_rejected() -> None:
    for value in ("", "   ", "abc def"):
        with pytest.raises(ValueError, match="bmc_partner_key"):
            RelaySettings(bmc_partner_key=SecretStr(value))


def _restamp(data: bytes, when: datetime) -> bytes:
    message = decode(data)
    message.header.timestamp = int(when.timestamp())
    for entity in message.entity:
        if entity.HasField("trip_update"):
            entity.trip_update.timestamp = message.header.timestamp
    return message.SerializeToString()


def test_disabled_feed_is_a_key_error() -> None:
    sncb = (feed(Operator.SNCB, FeedKind.TRIP_UPDATES),)
    plan = PollingPlan(trip_updates=timedelta(minutes=30))
    relay = make_relay(Portal(), Clock(), feeds=sncb, plan=plan)
    with pytest.raises(KeyError):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)


def test_construction_guards() -> None:
    fast = PollingPlan(trip_updates=timedelta(seconds=30), service_alerts=timedelta(seconds=30))
    with pytest.raises(QuotaExceededError):
        FeedRelay(REALTIME, fast, key=KEY)
    with pytest.raises(ValueError, match="realtime feeds only"):
        FeedRelay(FEEDS, PollingPlan(), key=KEY)


def test_missing_key_is_a_clear_startup_error() -> None:
    with pytest.raises(MissingKeyError, match="tempo_bmc_partner_key"):
        relay_from_settings(RelaySettings(_env_file=None))


def test_settings_with_key_enable_all_six_realtime_feeds() -> None:
    relay = relay_from_settings(RelaySettings(bmc_partner_key=KEY, alerts_seconds=900))
    for item in REALTIME:
        assert relay.enabled(item.operator, item.kind)


def test_settings_reject_an_over_quota_override() -> None:
    with pytest.raises(QuotaExceededError):
        relay_from_settings(
            RelaySettings(bmc_partner_key=KEY, trip_updates_seconds=30, alerts_seconds=30)
        )


def test_http_endpoints() -> None:
    portal, clock = Portal(), Clock()
    client = TestClient(create_relay_app(make_relay(portal, clock)))
    ok = client.get("/feeds/tec/trip-updates.pbf")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "application/x-protobuf"
    assert client.get("/feeds/stibmivb/trip-updates.pbf").status_code == 404
    assert client.get("/feeds/tec/vehicles.pbf").status_code == 404
    assert client.get("/feeds/nope/alerts.pbf").status_code == 404
    portal.status = 503
    assert client.get("/feeds/delijn/alerts.pbf").status_code == 503
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["requests_last_24h"] == 2
    assert KEY.get_secret_value() not in health.text
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_feed_selection_setting() -> None:
    settings = RelaySettings(
        bmc_partner_key=KEY,
        feeds="tec/trip-updates, delijn/alerts,tec/trip-updates",
    )
    relay = relay_from_settings(settings)
    assert relay.enabled(Operator.TEC, FeedKind.TRIP_UPDATES)
    assert relay.enabled(Operator.DE_LIJN, FeedKind.SERVICE_ALERTS)
    assert not relay.enabled(Operator.SNCB, FeedKind.TRIP_UPDATES)


def test_feed_selection_rejects_unknown_or_empty_values() -> None:
    with pytest.raises(ValueError, match="unknown realtime feed"):
        parse_feeds("stibmivb/trip-updates")
    with pytest.raises(ValueError, match="unknown realtime feed"):
        parse_feeds("tec/static")
    with pytest.raises(ValueError, match="no realtime feed"):
        parse_feeds(" , ")


def test_429_pauses_every_feed_because_the_quota_is_per_account() -> None:
    portal, clock = Portal(), Clock()
    portal.body_by_operator["nmbssncb"] = _restamp(DELIJN_TU, clock.now)
    relay = make_relay(portal, clock)
    sncb = relay.get(Operator.SNCB, FeedKind.TRIP_UPDATES)  # fresh copy before the 429
    portal.status, portal.headers = 429, {"Retry-After": "900"}
    with pytest.raises(RelayUnavailableError, match="429"):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    clock.now += timedelta(seconds=61)
    # Another feed is due but the account is paused: no request, the fresh copy is still served.
    assert relay.get(Operator.SNCB, FeedKind.TRIP_UPDATES) == sncb
    with pytest.raises(RelayUnavailableError, match="paused"):
        relay.get(Operator.DE_LIJN, FeedKind.TRIP_UPDATES)
    assert len(portal.requests) == 2
    assert relay.status()["paused_until"] is not None
    portal.status, portal.headers = 200, {}
    clock.now += timedelta(minutes=15)
    portal.body_by_operator["delijn"] = _restamp(DELIJN_TU, clock.now)
    assert relay.get(Operator.DE_LIJN, FeedKind.TRIP_UPDATES)
    assert relay.status()["paused_until"] is None


def test_retry_after_as_http_date_is_honoured() -> None:
    portal, clock = Portal(), Clock()
    until = clock.now + timedelta(minutes=40)
    portal.status = 429
    portal.headers = {"Retry-After": until.strftime("%a, %d %b %Y %H:%M:%S GMT")}
    relay = make_relay(portal, clock)
    with pytest.raises(RelayUnavailableError):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    clock.now += timedelta(minutes=39)
    with pytest.raises(RelayUnavailableError):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
    assert len(portal.requests) == 1


def test_rejected_key_is_reported_as_such() -> None:
    portal, clock = Portal(), Clock()
    portal.status = 401
    relay = make_relay(portal, clock)
    with pytest.raises(RelayUnavailableError, match="key rejected"):
        relay.get(Operator.TEC, FeedKind.TRIP_UPDATES)
