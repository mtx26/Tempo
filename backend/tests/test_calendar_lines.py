"""Journey titles and descriptions of the phone calendar feed: short and readable."""

from zoneinfo import ZoneInfo

from tempo.api.calendar_feed import _description, _title

TZ = ZoneInfo("Europe/Brussels")


def leg(mode: str, start: str, end: str, frm: str, to: str, line: str | None = None) -> dict:  # type: ignore[type-arg]
    return {
        "mode": mode,
        "line": line,
        "agency": "LETEC",
        "from": frm,
        "to": to,
        "start": f"2026-11-27T{start}:00+00:00",
        "end": f"2026-11-27T{end}:00+00:00",
    }


TRIP = {
    "type": "travel",
    "status": "routed",
    "title": "Trajet Maison → UMONS",
    "legs": [
        leg("ACCESS", "05:46", "05:56", "Maison", "Arrêt BEAUMONT Athénée"),
        leg("WALK", "05:56", "05:58", "START", "BEAUMONT Athénée"),
        leg("BUS", "05:58", "06:51", "BEAUMONT Athénée", "MONS Lycée", "E110"),
        leg("BUS", "06:55", "07:05", "MONS Lycée", "MONS Grand-Place", "18"),
        leg("WALK", "07:05", "07:15", "MONS Grand-Place", "END"),
    ],
}


def test_title_lists_only_the_lines() -> None:
    assert _title(TRIP) == "E110 → 18"
    assert _title({**TRIP, "legs": [leg("WALK", "05:00", "05:20", "a", "END")]}) == "À pied"
    assert _title({**TRIP, "status": "impossible"}) == "Trajet Maison → UMONS"
    assert _title({"type": "training", "title": "Footing"}) == "Footing"


def test_description_is_one_short_line_per_leg() -> None:
    assert _description(TRIP, TZ).splitlines() == [
        "Dépôt 10 min",
        "À pied 2 min",
        "E110 06:58 BEAUMONT Athénée → 07:51 MONS Lycée",
        "18 07:55 MONS Lycée → 08:05 MONS Grand-Place",
        "À pied 10 min",
    ]


def test_description_uses_reported_tracks() -> None:
    rail = leg("RAIL", "06:00", "06:45", "Mons", "Bruxelles", "IC")
    rail.update(departure_track="4", scheduled_departure_track="2", scheduled_arrival_track="8")
    assert _description({**TRIP, "legs": [rail]}, TZ) == (
        "IC 07:00 Mons (quai 4) → 07:45 Bruxelles (quai prévu 8)"
    )
