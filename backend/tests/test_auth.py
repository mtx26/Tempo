"""Token rotation and failed-authentication limiting of the private API."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tempo.api.app import create_app
from tempo.api.auth import MAX_TRACKED_CLIENTS, FailureLimiter, token_matches
from tempo.config import Settings

NEW = "Hq8Wm2Zr5Tx9Lb3Nc7Vd1Kf4Gs6Pj0YeUa"
OLD = "Zp4Qx8Rt2Wm6Yv0Lk3Nb7Hc1Df5Gj9SaEu"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_both_tokens_are_accepted_during_rotation() -> None:
    client = TestClient(create_app(Settings(api_token=NEW, api_token_previous=OLD)))
    assert client.get("/v1/status", headers=bearer(NEW)).status_code == 200
    assert client.get("/v1/status", headers=bearer(OLD)).status_code == 200
    assert client.get("/v1/status", headers=bearer(OLD + "x")).status_code == 401


def test_previous_token_stops_working_once_removed() -> None:
    client = TestClient(create_app(Settings(api_token=NEW)))
    assert client.get("/v1/status", headers=bearer(OLD)).status_code == 401


def test_previous_token_must_differ_and_be_strong() -> None:
    with pytest.raises(ValidationError):
        Settings(api_token=NEW, api_token_previous=NEW)
    with pytest.raises(ValidationError):
        Settings(api_token=NEW, api_token_previous="a" * 40)


def test_token_matches() -> None:
    accepted = (NEW.encode(), OLD.encode())
    assert token_matches(NEW, accepted)
    assert token_matches(OLD, accepted)
    assert not token_matches(None, accepted)
    assert not token_matches("", accepted)
    assert not token_matches("", (b"",))


def test_repeated_failures_lock_the_client_out_even_with_a_valid_token() -> None:
    client = TestClient(create_app(Settings(api_token=NEW)))
    for _ in range(9):
        assert client.get("/v1/status", headers=bearer("wrong" * 8)).status_code == 401
    assert client.get("/v1/status", headers=bearer("wrong" * 8)).status_code == 401  # 10th
    locked = client.get("/v1/status", headers=bearer(NEW))
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0
    assert NEW not in locked.text
    assert client.get("/health").status_code == 200  # public endpoint unaffected


def test_success_resets_the_failure_count() -> None:
    client = TestClient(create_app(Settings(api_token=NEW)))
    for _ in range(3):
        for _ in range(9):
            client.get("/v1/status", headers=bearer("wrong" * 8))
        assert client.get("/v1/status", headers=bearer(NEW)).status_code == 200


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 23, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_lockout_expires() -> None:
    clock = Clock()
    limiter = FailureLimiter(max_failures=3, clock=clock)
    for _ in range(3):
        limiter.failure("10.0.0.5")
    assert limiter.retry_after("10.0.0.5") == 300
    assert limiter.retry_after("10.0.0.6") is None
    clock.now += timedelta(minutes=5)
    assert limiter.retry_after("10.0.0.5") is None


def test_failures_outside_the_window_do_not_accumulate() -> None:
    clock = Clock()
    limiter = FailureLimiter(max_failures=3, clock=clock)
    for _ in range(5):
        limiter.failure("10.0.0.5")
        clock.now += timedelta(seconds=40)
        limiter.success("other")
    # never 3 failures within 60 s ... except the pairs 40 s apart: 2 per window
    assert limiter.retry_after("10.0.0.5") is None


def test_memory_stays_bounded_under_address_flood() -> None:
    limiter = FailureLimiter(max_failures=3, clock=Clock())
    for i in range(MAX_TRACKED_CLIENTS + 10):
        limiter.failure(f"client-{i}")
    assert len(limiter._failures) <= MAX_TRACKED_CLIENTS
