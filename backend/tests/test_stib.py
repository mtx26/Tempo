"""STIB realtime on REAL trimmed snapshots (2026-11-23, Belgian Mobility, CC BY 4.0)."""

import itertools
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx2
import pytest
from pydantic import SecretStr

from tempo.transport.feeds import KEY_HEADER
from tempo.transport.stib import (
    ANONYMOUS_DAILY_CAP,
    StibRealtime,
    StibSnapshot,
    StibUnavailableError,
    leg_advice,
    parse_travellers_information,
    parse_waiting_times,
    point_id,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stib"
WAITING = json.loads((FIXTURES / "waiting_times.json").read_text(encoding="utf-8"))
INFO = json.loads((FIXTURES / "travellers_information.json").read_text(encoding="utf-8"))
CET = timezone(timedelta(hours=1))


@pytest.mark.parametrize(
    ("stop_id", "point"),
    [
        ("stib_0470F", "0470"),
        ("0470", "0470"),
        ("stib_5022", "5022"),
        ("delijn_gs:delijn:3031", None),
        ("sncb_8814001", None),
        (None, None),
        ("stib_ABCD", None),
    ],
)
def test_point_id(stop_id: str | None, point: str | None) -> None:
    assert point_id(stop_id) == point


def test_real_waiting_times_and_messages_are_parsed() -> None:
    passages = parse_waiting_times(WAITING)
    first = passages[("3168", "59")][0]
    assert first.destination == "BORDET STATION" and first.message == "Ligne déviée"
    assert first.expected.utcoffset() == timedelta(hours=1)
    assert all(
        a.expected <= b.expected
        for values in passages.values()
        for a, b in itertools.pairwise(values)
    )
    messages = parse_travellers_information(INFO)
    assert messages[2].lines == frozenset({"51"}) and messages[2].points == frozenset(
        {"6659", "6650"}
    )
    assert messages[0].text.startswith("Travaux")


def test_malformed_entries_are_skipped_not_guessed() -> None:
    body = {
        "results": [
            {"pointid": "0501", "lineid": "4", "passingtimes": '[{"lineId": "4"}]'},
            {"pointid": "x", "passingtimes": "[]"},
        ]
    }
    assert parse_waiting_times(body) == {}


def test_leg_advice_uses_next_passage_line_message_and_disruptions() -> None:
    snapshot = StibSnapshot(parse_waiting_times(WAITING), parse_travellers_information(INFO))
    scheduled = datetime(2026, 11, 23, 23, 15, tzinfo=CET)
    notes = leg_advice(snapshot, "stib_3168", "59", scheduled)
    assert notes[0].startswith(
        "STIB temps réel : ligne 59 vers BORDET STATION, passage prévu à 23:18"
    )
    assert "Ligne déviée" in notes[1]
    works = leg_advice(snapshot, "stib_6659F", "51", scheduled)
    assert any("T51 interrompu" in n for n in works)
    assert leg_advice(snapshot, "sncb_8814001", "IC", scheduled) == []


def client(handler: object) -> httpx2.Client:
    return httpx2.Client(transport=httpx2.MockTransport(handler))  # type: ignore[arg-type]


def test_snapshot_is_cached_and_requests_are_capped() -> None:
    calls: list[httpx2.Request] = []

    def ok(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json=WAITING if "Waiting" in request.url.path else INFO)

    now = [datetime(2026, 11, 23, 22, tzinfo=UTC)]
    stib = StibRealtime(http_client=client(ok), clock=lambda: now[0])
    stib.snapshot()
    now[0] += timedelta(minutes=10)
    stib.snapshot()
    assert len(calls) == 2  # one snapshot = 2 requests, then served from cache
    assert KEY_HEADER not in calls[0].headers
    for _ in range(ANONYMOUS_DAILY_CAP):
        now[0] += timedelta(minutes=16)
        try:
            stib.snapshot()
        except StibUnavailableError as exc:
            assert "budget" in str(exc)
            break
    assert stib.requests_last_24h <= ANONYMOUS_DAILY_CAP


def test_key_header_and_errors() -> None:
    seen: list[httpx2.Request] = []

    def fail(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(503)

    stib = StibRealtime(key=SecretStr("k" * 32), http_client=client(fail))
    with pytest.raises(StibUnavailableError, match="503"):
        stib.snapshot()
    assert seen[0].headers[KEY_HEADER] == "k" * 32

    def garbage(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"<html>")

    with pytest.raises(StibUnavailableError, match="JSON"):
        StibRealtime(http_client=client(garbage)).snapshot()


def test_unexpected_payload_shapes_never_break_the_caller() -> None:
    def weird(request: httpx2.Request) -> httpx2.Response:
        if "Waiting" in request.url.path:
            return httpx2.Response(
                200, json={"results": [1, "x", {"pointid": "0501", "passingtimes": "{not json"}]}
            )
        return httpx2.Response(200, json={"results": [{"content": [["x"]], "priority": [1]}]})

    with pytest.raises(StibUnavailableError, match="unexpected"):
        StibRealtime(http_client=client(weird)).snapshot()
