"""Tool results reach the model in local time (it misread UTC times as local ones)."""

from zoneinfo import ZoneInfo

from tempo.assistant.agent import local_times

TZ = ZoneInfo("Europe/Brussels")


def test_timestamps_are_given_to_the_model_in_local_time() -> None:
    result = {
        "ok": True,
        "result": {
            "items": [{"start": "2026-12-02T12:30:00+00:00", "title": "Systèmes d'exploitation"}],
            "legs": [{"line": "E110", "start": "2026-11-27T06:17:00+00:00"}],
            "day": "2026-12-02",
            "note": "12:30",
        },
    }
    out = local_times(result, TZ)
    assert out["result"]["items"][0]["start"] == "2026-12-02T13:30:00+01:00"
    assert out["result"]["legs"][0]["start"] == "2026-11-27T07:17:00+01:00"
    assert out["result"]["day"] == "2026-12-02" and out["result"]["note"] == "12:30"
    assert out["result"]["items"][0]["title"] == "Systèmes d'exploitation"
