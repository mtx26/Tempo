"""Service alerts on legs: parsing of a REAL MOTIS answer, relevance in time, travel warnings.

Fixture ``motis_plan_v6_real_belgium_alerts.json``: real MOTIS v2.11.4 answer (nationwide data,
TEC + SNCB + De Lijn GTFS-RT trip updates and alerts of 2026-11-23, TEC repaired), Bastogne ->
Liège on Tuesday 2026-11-24, geometry removed and alerts truncated to 4 per leg.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2

from tempo.transport.location import LocatedEvent
from tempo.transport.motis import MotisClient, _alert
from tempo.transport.places import Coordinates, Place
from tempo.transport.routing import Journey, Leg, ServiceAlert
from tempo.transport.travel import plan_day_travel

REAL: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "motis_plan_v6_real_belgium_alerts.json").read_text(
        encoding="utf-8"
    )
)
BASTOGNE = Coordinates(50.0003, 5.7155)
LIEGE = Coordinates(50.6245, 5.5668)


def real_journeys() -> list[Journey]:
    http = httpx2.Client(transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=REAL)))
    client = MotisClient("http://tempo-routing:8080", http_client=http)
    return client.plan(BASTOGNE, LIEGE, datetime(2026, 11, 24, 7, tzinfo=UTC), arrive_by=False)


def test_real_alerts_are_parsed_on_transit_legs() -> None:
    journeys = real_journeys()
    transit = [leg for j in journeys for leg in j.legs if leg.is_transit]
    assert transit
    assert all(leg.alerts for leg in transit)
    alert = transit[0].alerts[0]
    assert alert.header
    assert alert.effect in {"REDUCED_SERVICE", "UNKNOWN_EFFECT", "NO_SERVICE"}


def test_alerts_outside_their_impact_period_are_not_relevant() -> None:
    """MOTIS attached the 23 November strike notices to legs on 24 November."""
    tec_legs = [leg for j in real_journeys() for leg in j.legs if leg.agency == "LETEC"]
    strike = [a for leg in tec_legs for a in leg.alerts if a.cause == "STRIKE"]
    assert strike
    assert all(a.impact_periods for a in strike)
    for leg in tec_legs:
        assert not [a for a in leg.relevant_alerts if a.cause == "STRIKE"]


def test_alerts_without_period_stay_relevant_and_are_deduplicated() -> None:
    journeys = real_journeys()
    sncb = [j for j in journeys if any(leg.agency == "NMBS/SNCB" for leg in j.legs)]
    assert sncb
    disruptions = sncb[0].disruptions
    headers = [a.header for a in disruptions]
    assert len(headers) == len(set(headers))
    assert any(not a.impact_periods for a in disruptions)


# --- synthetic, for the selection and warnings rules ---------------------------------------

HOME = Place("home", "Kot", Coordinates(50.668, 4.612))
CAMPUS = Place("campus", "Campus", Coordinates(50.85, 4.35))
DAY = datetime(2026, 12, 1, tzinfo=UTC)
TODAY = ((DAY, DAY + timedelta(days=1)),)
NO_SERVICE = ServiceAlert("Ligne interrompue", effect="NO_SERVICE", impact_periods=TODAY)
UNTIMED = ServiceAlert("Walcourt - Couvin : Aucun train", effect="NO_SERVICE")
INFO = ServiceAlert("Travaux le week-end", effect="OTHER_EFFECT")


def journey(
    dep_h: int,
    dep_m: int,
    *,
    alerts: tuple[ServiceAlert, ...] = (),
    delay: int = 0,
    transfers: int = 0,
    realtime: bool = False,
) -> Journey:
    start = DAY.replace(hour=dep_h, minute=dep_m)
    end = start + timedelta(minutes=40)
    shift = timedelta(minutes=delay)
    leg = Leg(
        "BUS",
        "a",
        "b",
        start + shift,
        end + shift,
        start,
        end,
        realtime=realtime,
        line="E69",
        alerts=alerts,
    )
    return Journey((leg,), start + shift, end + shift, transfers, "test")


class Fixed:
    def __init__(self, journeys: list[Journey]) -> None:
        self.journeys = journeys

    def plan(
        self, origin: Coordinates, destination: Coordinates, time: datetime, *, arrive_by: bool
    ) -> list[Journey]:
        return self.journeys


def lecture() -> LocatedEvent:
    return LocatedEvent(DAY.replace(hour=9), DAY.replace(hour=11), CAMPUS)


def test_alert_period_overlap_rules() -> None:
    start, end = DAY.replace(hour=8), DAY.replace(hour=9)
    before = ServiceAlert("x", impact_periods=((None, start),))
    open_end = ServiceAlert("x", impact_periods=((start - timedelta(days=1), None),))
    later = ServiceAlert("x", impact_periods=((end, end + timedelta(hours=1)),))
    assert not before.active_during(start, end)
    assert open_end.active_during(start, end)
    assert not later.active_during(start, end)
    assert ServiceAlert("x").active_during(start, end)


def test_alerts_break_ties_but_never_exclude_a_journey() -> None:
    disrupted = journey(8, 10, alerts=(NO_SERVICE,))
    clean = journey(8, 10, transfers=1)
    plan = plan_day_travel([lecture()], Fixed([disrupted, clean]), start_place=HOME)
    assert plan.blocks[0].journey is clean
    only_disrupted = plan_day_travel([lecture()], Fixed([disrupted]), start_place=HOME)
    assert only_disrupted.blocks[0].journey is disrupted
    assert not only_disrupted.issues
    assert only_disrupted.warnings == [
        "perturbation NO_SERVICE : Ligne interrompue (Kot -> Campus (2026-12-01T08:50:00+00:00))",
    ]


def test_later_departure_still_wins_over_fewer_alerts() -> None:
    early_clean = journey(7, 30)
    late_disrupted = journey(8, 10, alerts=(NO_SERVICE,))
    plan = plan_day_travel([lecture()], Fixed([early_clean, late_disrupted]), start_place=HOME)
    assert plan.blocks[0].journey is late_disrupted


def test_informational_alerts_and_small_delays_are_not_warned() -> None:
    plan = plan_day_travel(
        [lecture()],
        Fixed([journey(8, 0, alerts=(INFO,), delay=1, realtime=True)]),
        start_place=HOME,
    )
    assert plan.warnings == []


def test_live_delay_is_reported_and_travel_uses_live_times() -> None:
    late = journey(8, 0, delay=7, realtime=True)
    plan = plan_day_travel([lecture()], Fixed([late]), start_place=HOME)
    block = plan.blocks[0]
    assert block.start == DAY.replace(hour=8, minute=7)
    assert plan.warnings == [
        "retard en temps réel +7 min sur E69 (Kot -> Campus (2026-12-01T08:50:00+00:00))"
    ]


def test_live_delay_that_breaks_the_buffer_makes_the_journey_infeasible() -> None:
    too_late = journey(8, 5, delay=10, realtime=True)  # now arrives 08:55 > 08:50 deadline
    plan = plan_day_travel([lecture()], Fixed([too_late]), start_place=HOME)
    assert plan.blocks[0].journey is None
    assert plan.issues and "impossible connection" in plan.issues[0]


def test_return_trip_warnings() -> None:
    back = journey(11, 5, alerts=(NO_SERVICE,))
    plan = plan_day_travel([lecture()], Fixed([back]), start_place=CAMPUS, end_place=HOME)
    assert plan.blocks[0].journey is back
    assert plan.warnings and plan.warnings[0].startswith("perturbation NO_SERVICE")


def test_untimed_severe_notices_are_summarised_once_per_plan() -> None:
    """Real SNCB notices have no period and are attached by MOTIS to every SNCB leg."""
    there = journey(8, 0, alerts=(UNTIMED, UNTIMED, INFO))
    back = journey(11, 5, alerts=(UNTIMED,))
    plan = plan_day_travel([lecture()], Fixed([there, back]), start_place=HOME, end_place=HOME)
    assert plan.warnings == [
        "avis d'exploitant sans période d'impact (BUS) : non vérifiés pour les trajets prévus",
    ]


def test_distinct_alerts_with_the_same_header_are_kept() -> None:
    timed = ServiceAlert("Travaux", effect="DETOUR", impact_periods=TODAY)
    untimed = ServiceAlert("Travaux", effect="DETOUR")
    other_text = ServiceAlert("Travaux", description="autre ligne", effect="DETOUR")
    j = journey(8, 0, alerts=(timed, untimed, other_text, timed))
    assert len(j.disruptions) == 3
    assert j.severe_disruptions == 1


def test_epoch_zero_period_bound_means_unbounded() -> None:
    alert = _alert(
        {
            "headerText": "x",
            "effect": "NO_SERVICE",
            "impactPeriod": [{"start": "2026-11-23T00:00:00Z", "end": "1970-01-01T00:00:00Z"}],
        }
    )
    assert alert.impact_periods == ((datetime(2026, 11, 23, tzinfo=UTC), None),)
    assert alert.active_during(DAY, DAY + timedelta(hours=1))


def test_arrival_delay_is_reported() -> None:
    start = DAY.replace(hour=8)
    leg = Leg(
        "BUS",
        "a",
        "b",
        start,
        start + timedelta(minutes=46),
        start,
        start + timedelta(minutes=40),
        realtime=True,
        line="E69",
    )
    late_arrival = Journey((leg,), start, start + timedelta(minutes=46), 0, "test")
    assert late_arrival.max_delay == timedelta(minutes=6)
    plan = plan_day_travel([lecture()], Fixed([late_arrival]), start_place=HOME)
    assert plan.warnings == [
        "retard en temps réel +6 min sur E69 (Kot -> Campus (2026-12-01T08:50:00+00:00))"
    ]


def test_untimed_notices_do_not_bias_the_choice() -> None:
    with_notice = journey(8, 10, alerts=(UNTIMED,))
    other = journey(8, 10, transfers=1)
    plan = plan_day_travel([lecture()], Fixed([with_notice, other]), start_place=HOME)
    assert plan.blocks[0].journey is with_notice


def test_real_sncb_notices_are_untimed() -> None:
    sncb = [j for j in real_journeys() if any(leg.agency == "NMBS/SNCB" for leg in j.legs)]
    assert sncb[0].untimed_disruptions
    assert all(a.impact_periods for a in sncb[0].timed_disruptions)
