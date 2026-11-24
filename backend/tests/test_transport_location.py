"""Places, phone position freshness and future-trip origin inference (ARCHITECTURE.md).

Rules under test: current location only from the configured HA phone tracker, checked for
freshness and accuracy; future origins come from the event chain or an explicit declaration,
never from today's (stale) GPS; when uncertain, ask instead of guessing.
"""

from datetime import UTC, datetime, timedelta

import pytest

from tempo.transport.location import (
    DeclaredStay,
    LocatedEvent,
    OriginSource,
    PhonePosition,
    PositionStatus,
    assess_position,
    infer_origin,
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


# --- origin inference ------------------------------------------------------------------------


def event(place: Place, start_h: int, end_h: int, day: int = 1) -> LocatedEvent:
    return LocatedEvent(
        start=datetime(2026, 12, day, start_h, tzinfo=UTC),
        end=datetime(2026, 12, day, end_h, tzinfo=UTC),
        place=place,
    )


def test_origin_is_previous_event_of_the_same_day() -> None:
    chain = [event(CAMPUS, 8, 10)]
    result = infer_origin(
        departure=datetime(2026, 12, 1, 16, tzinfo=UTC),
        chain=chain,
        position=position(),
        stays=[],
        now=NOW,
    )
    assert result.source is OriginSource.PREVIOUS_EVENT
    assert result.place == CAMPUS


def test_near_term_departure_uses_fresh_phone_position() -> None:
    result = infer_origin(
        departure=NOW + timedelta(minutes=45), chain=[], position=position(), stays=[], now=NOW
    )
    assert result.source is OriginSource.CURRENT_POSITION
    assert result.coordinates == position().coordinates


def test_future_departure_never_uses_todays_gps() -> None:
    result = infer_origin(
        departure=NOW + timedelta(days=2), chain=[], position=position(), stays=[], now=NOW
    )
    assert result.source is OriginSource.NEEDS_CLARIFICATION
    assert result.coordinates is None


def test_declared_stay_is_used_for_future_departure() -> None:
    stay = DeclaredStay(place=HOME, start=NOW + timedelta(days=1), end=NOW + timedelta(days=3))
    result = infer_origin(
        departure=NOW + timedelta(days=2), chain=[], position=position(), stays=[stay], now=NOW
    )
    assert result.source is OriginSource.DECLARED_STAY
    assert result.place == HOME


def test_stale_position_for_near_term_departure_needs_clarification() -> None:
    result = infer_origin(
        departure=NOW + timedelta(minutes=30),
        chain=[],
        position=position(age_minutes=60),
        stays=[],
        now=NOW,
    )
    assert result.source is OriginSource.NEEDS_CLARIFICATION
    assert "stale" in result.reason


def test_previous_event_on_another_day_is_not_used() -> None:
    result = infer_origin(
        departure=datetime(2026, 12, 2, 9, tzinfo=UTC),
        chain=[event(CAMPUS, 8, 10, day=1)],
        position=None,
        stays=[],
        now=NOW,
    )
    assert result.source is OriginSource.NEEDS_CLARIFICATION


def test_event_still_running_at_departure_is_not_an_origin() -> None:
    result = infer_origin(
        departure=datetime(2026, 12, 1, 9, tzinfo=UTC),
        chain=[event(CAMPUS, 8, 10)],
        position=None,
        stays=[],
        now=NOW,
    )
    assert result.source is OriginSource.NEEDS_CLARIFICATION
