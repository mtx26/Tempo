"""Places, phone position freshness and the day's base (ARCHITECTURE.md §7).

Rules under test: current location only from the configured HA phone tracker, checked for
freshness and accuracy; future days start from the planned location, else home; when
ambiguous, ask instead of guessing.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tempo.transport.location import (
    Alternation,
    DeclaredStay,
    PhonePosition,
    PositionStatus,
    assess_position,
    base_at,
)
from tempo.transport.places import Coordinates, Place, PlaceDirectory

NOW = datetime(2026, 12, 1, 7, 0, tzinfo=UTC)
HOME = Place("home", "Kot", Coordinates(50.668, 4.612))
CAMPUS = Place("campus", "Campus", Coordinates(50.669, 4.615))
POOL = Place("pool", "Piscine", Coordinates(50.85, 4.35))


def directory() -> PlaceDirectory:
    return PlaceDirectory(
        places=[HOME, CAMPUS, POOL],
        aliases={"Auditoire A1": "campus", "auditoire b2": "campus", "Piscine Poseidon": "pool"},
    )


def position(age_minutes: int = 2, accuracy: float = 25.0) -> PhonePosition:
    return PhonePosition(
        coordinates=Coordinates(50.7, 4.6),
        accuracy_m=accuracy,
        observed_at=NOW - timedelta(minutes=age_minutes),
        entity_id="device_tracker.phone",
    )


# --- places -------------------------------------------------------------------------------


def test_alias_resolution_is_exact_and_case_insensitive() -> None:
    assert directory().resolve("AUDITOIRE B2") == CAMPUS
    assert directory().resolve("  Auditoire A1 ") == CAMPUS


def test_unknown_location_text_is_not_guessed() -> None:
    assert directory().resolve("Auditoire A") is None
    assert directory().resolve(None) is None


def test_alias_to_unknown_place_is_rejected() -> None:
    with pytest.raises(ValueError):
        PlaceDirectory(places=[HOME], aliases={"x": "missing"})


@pytest.mark.parametrize(("lat", "lon"), [(91, 0), (0, 181), (-91, 0)])
def test_invalid_coordinates_are_rejected(lat: float, lon: float) -> None:
    with pytest.raises(ValueError):
        Coordinates(lat, lon)


# --- phone position -------------------------------------------------------------------------


def test_recent_accurate_position_is_fresh() -> None:
    assert assess_position(position(), NOW) is PositionStatus.FRESH


def test_old_position_is_stale() -> None:
    assert assess_position(position(age_minutes=30), NOW) is PositionStatus.STALE


def test_imprecise_position_is_inaccurate() -> None:
    assert assess_position(position(accuracy=800), NOW) is PositionStatus.INACCURATE


def test_missing_position_is_unavailable() -> None:
    assert assess_position(None, NOW) is PositionStatus.UNAVAILABLE


def test_position_from_the_future_is_not_trusted() -> None:
    assert assess_position(position(age_minutes=-10), NOW) is PositionStatus.STALE


# --- base at an instant ---------------------------------------------------------------------


ALT = Alternation((HOME,), date(2026, 11, 27), 7, ZoneInfo("Europe/Brussels"))


def test_base_is_the_planned_location_covering_the_instant_else_the_residence() -> None:
    weekend = DeclaredStay(
        POOL, datetime(2026, 12, 2, 18, tzinfo=UTC), datetime(2026, 12, 4, tzinfo=UTC)
    )
    assert base_at(datetime(2026, 12, 3, 12, tzinfo=UTC), [weekend], ALT) == POOL
    assert base_at(datetime(2026, 12, 3, 12, tzinfo=UTC), [], ALT) == HOME
    # The base changes during a day: home in the morning, the pool from the evening on.
    evening = DeclaredStay(
        POOL, datetime(2026, 12, 3, 18, tzinfo=UTC), datetime(2026, 12, 4, tzinfo=UTC)
    )
    assert base_at(datetime(2026, 12, 3, 8, tzinfo=UTC), [evening], ALT) == HOME
    assert base_at(datetime(2026, 12, 3, 19, tzinfo=UTC), [evening], ALT) == POOL


def test_overlapping_planned_locations_are_ambiguous_not_guessed() -> None:
    stays = [
        DeclaredStay(HOME, datetime(2026, 12, 2, tzinfo=UTC), datetime(2026, 12, 4, tzinfo=UTC)),
        DeclaredStay(POOL, datetime(2026, 12, 3, 6, tzinfo=UTC), datetime(2026, 12, 4, tzinfo=UTC)),
    ]
    assert base_at(datetime(2026, 12, 3, 12, tzinfo=UTC), stays, ALT) is None
