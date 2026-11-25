"""End-to-end planning pipeline on a realistic week (fake routing: 30 min between places)."""

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tempo.domain.models import TargetKind
from tempo.planning.engine import (
    Commitment,
    CommitmentKind,
    OutcomeStatus,
    PlanningInput,
    TrainingRequest,
    run_planning,
)
from tempo.scheduler.model import PlannedBlock, StudyDemand
from tempo.timeutil import Interval, utc
from tempo.transport.location import DeclaredStay
from tempo.transport.places import Coordinates, Place
from tempo.transport.routing import Journey, Leg, RoutingUnavailableError
from tempo.transport.travel import StudyWindow, TravelStatus

TZ = ZoneInfo("Europe/Brussels")
HOME = Place("home", "Kot", Coordinates(50.668, 4.612))
CAMPUS = Place("campus", "Campus", Coordinates(50.85, 4.35))
CLUB = Place("club", "Club", Coordinates(50.70, 4.60))
MUM = Place("mum", "Maman", Coordinates(50.40, 4.40))
TRIP = timedelta(minutes=30)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 12, day, hour, minute, tzinfo=TZ)


class FakeRouting:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def plan(
        self, origin: Coordinates, destination: Coordinates, time: datetime, *, arrive_by: bool
    ) -> list[Journey]:
        if self.fail:
            raise RoutingUnavailableError("MOTIS down")
        start = time - TRIP if arrive_by else time
        leg = Leg("BUS", "a", "b", start, start + TRIP, start, start + TRIP, realtime=False)
        return [Journey((leg,), start, start + TRIP, 0, "fake")]


def week(**overrides: object) -> PlanningInput:
    base: dict[str, object] = {
        "now": at(7, 6),
        "horizon_end": at(13, 23),
        "timezone": TZ,
        "home": HOME,
        "commitments": [
            Commitment(f"class-{d}", "Analyse", at(d, 10), at(d, 12), CommitmentKind.CLASS, CAMPUS)
            for d in (7, 8, 9, 10, 11)
        ]
        + [Commitment("race", "10 km", at(12, 10), at(12, 11), CommitmentKind.RACE, CLUB)],
        "trainings": [
            TrainingRequest(
                "t1",
                "Fractionné",
                timedelta(minutes=60),
                at(8, 0),
                at(9, 23),
                place=CLUB,
                preferred_start=at(8, 18),
            )
        ],
        "availability": [StudyWindow(Interval(at(d, 8), at(d, 20)), HOME) for d in range(7, 14)],
        "demands": [
            StudyDemand("maths", "maths", 360, at(12, 23, 59)),
            StudyDemand("physique", "physique", 270, at(13, 23, 59), TargetKind.HARD),
        ],
    }
    base.update(overrides)
    return PlanningInput(**base)  # type: ignore[arg-type]


def overlaps(a: Interval, items: list[Interval]) -> bool:
    return any(a.overlaps(i) for i in items)


def test_realistic_week_is_complete_and_consistent() -> None:
    outcome = run_planning(week(), FakeRouting())
    assert outcome.status is OutcomeStatus.COMPLETE, outcome.issues
    assert outcome.result is not None
    training = outcome.trainings[0]
    assert training.start == at(8, 18)
    assert training.reserved == Interval(at(8, 17, 30), at(8, 19, 30))
    travel = [Interval(b.start, b.end) for b in outcome.travel if b.end > b.start]
    assert all(b.status is TravelStatus.ROUTED for b in outcome.travel)
    # Every class day has an outbound trip arriving 10 min early, and a return.
    for d in (7, 8, 9, 10, 11):
        assert any(b.end == at(d, 10) and b.destination == CAMPUS for b in outcome.travel)
        assert any(b.start == at(d, 12) and b.destination == HOME for b in outcome.travel)
    busy = travel + [training.reserved] + [Interval(at(d, 10), at(d, 12)) for d in range(7, 12)]
    for block in outcome.result.blocks:
        assert not overlaps(block.interval, busy)
    assert outcome.issues == []


def test_unknown_location_blocks_time_and_is_reported() -> None:
    odd = Commitment("x", "TP", at(9, 14), at(9, 16), CommitmentKind.CLASS, None, "Salle Z.99")
    outcome = run_planning(week(commitments=[odd], trainings=[]), FakeRouting())
    assert any("unknown location 'Salle Z.99'" in i for i in outcome.issues)
    assert outcome.result is not None
    assert not overlaps(Interval(at(9, 14), at(9, 16)), [b.interval for b in outcome.result.blocks])


def test_routing_outage_blocks_gaps_and_reports_instead_of_inventing() -> None:
    outcome = run_planning(week(), FakeRouting(fail=True))
    assert outcome.status in (OutcomeStatus.COMPLETE, OutcomeStatus.PARTIAL)
    assert any("routing unavailable" in i for i in outcome.issues)
    assert {b.status for b in outcome.travel} == {TravelStatus.UNKNOWN}
    training = outcome.trainings[0]
    # No invented travel duration: nothing is reserved around the session, it is reported.
    assert training.reserved == Interval(training.start, training.end)
    assert any("travel not reserved" in i for i in outcome.issues)


def test_base_may_change_during_a_day() -> None:
    """D03: leave from dad's in the morning, go back to mum's after class."""
    dad = Place("dad", "Papa", Coordinates(50.50, 4.50))
    stays = [DeclaredStay(dad, at(8, 0), at(8, 11)), DeclaredStay(MUM, at(8, 11), at(9, 0))]
    only_tuesday = [
        Commitment("class-8", "Analyse", at(8, 10), at(8, 12), CommitmentKind.CLASS, CAMPUS)
    ]
    outcome = run_planning(week(commitments=only_tuesday, trainings=[], stays=stays), FakeRouting())
    outbound = [b for b in outcome.travel if b.destination == CAMPUS]
    back = [b for b in outcome.travel if b.origin == CAMPUS]
    assert outbound and outbound[0].origin == dad
    assert back and back[0].destination == MUM


def test_unplaced_activity_makes_the_plan_partial() -> None:
    impossible = TrainingRequest(
        "t9", "Sortie", timedelta(hours=3), at(10, 10), at(10, 12), place=None
    )
    outcome = run_planning(week(trainings=[impossible]), FakeRouting())
    assert outcome.unplaced_trainings == ["t9"]
    assert outcome.status is OutcomeStatus.PARTIAL


def test_strict_task_session_that_does_not_fit_is_a_hard_miss() -> None:
    request = TrainingRequest(
        "s1",
        "Dossier",
        timedelta(hours=20),
        at(7, 6),
        at(8, 0),
        place=CAMPUS,
        task_id="dossier",
        target=at(8, 0),
        strict=True,
    )
    outcome = run_planning(week(trainings=[request]), FakeRouting())
    assert "dossier" in outcome.hard_deadline_misses
    assert outcome.unplanned_minutes["dossier"] == 20 * 60
    assert outcome.status is OutcomeStatus.PARTIAL


def test_overlapping_activity_is_rejected_by_the_independent_validator() -> None:
    from tempo.scheduler.validate import Occupied, PlacedSession, validate_schedule

    session = PlacedSession("activity:x", "Sortie", at(8, 10), at(8, 11), at(8, 8), at(8, 20))
    found = validate_schedule(
        [session], [Occupied("Analyse", Interval(at(8, 10), at(8, 12)))], [], TZ
    )
    assert found and "overlaps Analyse" in found[0]


def test_declared_stay_changes_the_day_base_without_automatic_switching() -> None:
    stay = DeclaredStay(MUM, at(12, 0), at(13, 0))
    outcome = run_planning(week(stays=[stay]), FakeRouting())
    race_trips = [b for b in outcome.travel if b.destination == CLUB and b.end.day == 12]
    assert race_trips and race_trips[0].origin == MUM
    assert all(b.origin != MUM for b in outcome.travel if b.start < at(12, 0))


def test_past_commitments_and_locked_blocks() -> None:
    locked = PlannedBlock("maths", "maths", at(9, 14), at(9, 15, 30), locked=True)
    outcome = run_planning(week(now=at(9, 13), locked_blocks=[locked]), FakeRouting())
    assert outcome.result is not None
    assert locked in outcome.result.blocks
    assert all(utc(b.start) >= utc(at(9, 13)) for b in outcome.result.blocks)
    assert all(b.end > at(9, 13) for b in outcome.travel)


def test_training_without_slot_is_reported() -> None:
    blocked = Commitment("exam", "Examen", at(8, 0), at(9, 23, 30), CommitmentKind.OTHER, None)
    outcome = run_planning(week(commitments=[blocked]), FakeRouting())
    assert outcome.unplaced_trainings == ["t1"]
    assert any("aucun créneau libre" in i for i in outcome.issues)


def test_impossible_hard_deadline_is_partial_not_invalid() -> None:
    demands = [StudyDemand("huge", "huge", 6000, at(8, 23), TargetKind.HARD)]
    outcome = run_planning(week(demands=demands), FakeRouting())
    assert outcome.status is OutcomeStatus.PARTIAL
    assert outcome.result is not None and outcome.result.hard_deadline_misses == ["huge"]


def test_long_gap_means_going_home_between_commitments() -> None:
    outcome = run_planning(week(), FakeRouting())
    day8 = sorted((b for b in outcome.travel if b.start.day == 8), key=lambda b: b.start)
    legs = [(b.origin.id, b.destination.id) for b in day8]
    assert legs == [("home", "campus"), ("campus", "home"), ("home", "club"), ("club", "home")]


def test_free_minutes_before_a_deadline_counts_overlapping_busy_time_once() -> None:
    from tempo.planning.engine import free_minutes

    window = [Interval(at(7, 8), at(7, 20))]
    busy = [Interval(at(7, 10), at(7, 12)), Interval(at(7, 11), at(7, 13))]
    assert free_minutes(window, busy, at(7, 23)) == 720 - 180
    assert free_minutes(window, busy, at(7, 15)) == 420 - 180
    assert free_minutes(window, busy, at(7, 7)) == 0


def test_overlapping_planned_locations_are_reported_not_guessed() -> None:
    stays = [DeclaredStay(MUM, at(11, 0), at(13, 0)), DeclaredStay(HOME, at(12, 6), at(12, 23))]
    outcome = run_planning(week(stays=stays), FakeRouting())
    assert any("planned locations overlap at 2026-12-12" in i for i in outcome.issues)
    assert not [b for b in outcome.travel if b.start.day == 12 and b.origin == HOME]


def test_mandatory_travel_blocks_keep_their_alternatives() -> None:
    class Several(FakeRouting):
        """Three journeys per answer, 15 min apart."""

        def plan(self, origin, destination, time, *, arrive_by):  # type: ignore[no-untyped-def]
            first = super().plan(origin, destination, time, arrive_by=arrive_by)[0]
            step = timedelta(minutes=-15 if arrive_by else 15)
            return [
                Journey(
                    tuple(
                        replace(
                            leg,
                            start=leg.start + k * step,
                            end=leg.end + k * step,
                            scheduled_start=leg.start + k * step,
                            scheduled_end=leg.end + k * step,
                        )
                        for leg in first.legs
                    ),
                    first.departure + k * step,
                    first.arrival + k * step,
                    0,
                    "fake",
                )
                for k in range(3)
            ]

    outcome = run_planning(week(), Several())
    routed = [b for b in outcome.travel if b.journey is not None]
    assert routed and all(b.alternatives for b in routed)
    assert all(b.journey not in b.alternatives for b in routed)


def _day9_legs(second_start: datetime) -> list[tuple[str, str]]:
    commitments = [
        Commitment("c", "Analyse", at(9, 10), at(9, 12), CommitmentKind.CLASS, CAMPUS),
        Commitment(
            "m",
            "Réunion",
            second_start,
            second_start + timedelta(hours=1),
            CommitmentKind.OTHER,
            CLUB,
        ),
    ]
    outcome = run_planning(week(commitments=commitments, trainings=[]), FakeRouting())
    day9 = sorted((b for b in outcome.travel if b.start.day == 9), key=lambda b: b.start)
    return [(b.origin.id, b.destination.id) for b in day9]


def test_going_home_between_commitments_only_when_worth_it() -> None:
    """30 min trips, 10 min margin: 2 h 30 between them leaves 1 h 20 at home (go home);
    1 h 40 leaves 30 min (go directly). No fixed 3 h rule."""
    assert _day9_legs(at(9, 14, 30)) == [
        ("home", "campus"),
        ("campus", "home"),
        ("home", "club"),
        ("club", "home"),
    ]
    assert _day9_legs(at(9, 13, 40)) == [("home", "campus"), ("campus", "club"), ("club", "home")]


def test_journey_of_a_flexible_activity_over_a_commitment_is_a_violation() -> None:
    from tempo.scheduler.validate import Occupied, validate_schedule

    trip = Occupied("Kot -> Club", Interval(at(8, 17), at(8, 18)))
    found = validate_schedule(
        [], [Occupied("Analyse", Interval(at(8, 17, 30), at(8, 19)))], [], TZ, [trip]
    )
    assert found == ["journey Kot -> Club overlaps Analyse"]


def test_find_conflicts_covers_every_category_and_never_hides_one() -> None:
    from tempo.planning.engine import ConflictKind, find_conflicts

    exam = Commitment("event:1", "Examen", at(9, 9), at(9, 11), CommitmentKind.CLASS, CAMPUS)
    race = Commitment("activity:r", "10 km", at(9, 10), at(9, 11), CommitmentKind.RACE, CLUB)
    locked = Commitment(
        "activity:t", "Footing", at(9, 10, 30), at(9, 11, 30), CommitmentKind.TRAINING, movable=True
    )
    running = Commitment(
        "activity:u", "Sortie", at(9, 10, 45), at(9, 12), CommitmentKind.OTHER, underway=True
    )
    block = PlannedBlock("maths", "maths", at(9, 8), at(9, 9, 30), locked=True, ref="block:b1")
    computed = PlannedBlock("maths", "maths", at(9, 8), at(9, 9, 30), locked=True)
    found = find_conflicts([exam, race, locked, running], [block, computed], at(9, 6))
    kinds = {(c.kind, c.subject_ref, c.other.ref) for c in found}
    # Two fixed commitments: reported even though nothing can be resolved automatically.
    assert (ConflictKind.FIXED, "event:1", "activity:r") in kinds
    # A locked flexible activity is the subject of an approval, never the under-way one.
    assert (ConflictKind.LOCKED_ACTIVITY, "activity:t", "event:1") in kinds
    assert not any(
        c.subject_ref == "activity:u" and c.kind is not ConflictKind.FIXED for c in found
    )
    # Stored locked block vs commitment; a computed (under-way) block is not a protection.
    assert [c.subject_ref for c in found if c.kind is ConflictKind.LOCKED_BLOCK] == ["block:b1"]


def test_conflicts_are_reported_even_without_a_usable_plan() -> None:
    clash = [
        Commitment("a", "Cours", at(9, 10), at(9, 12), CommitmentKind.CLASS, None),
        Commitment("b", "Dentiste", at(9, 11), at(9, 12), CommitmentKind.OTHER, None),
    ]
    outcome = run_planning(week(commitments=clash, trainings=[]), FakeRouting())
    assert next(c.describe() for c in outcome.conflicts).startswith("fixed: class 'Cours'")


def _residence_training(**kw: object) -> TrainingRequest:
    values: dict[str, object] = {
        "activity_id": "run",
        "title": "Endurance",
        "duration": timedelta(minutes=60),
        "earliest_start": at(8, 0),
        "latest_end": at(8, 23, 59),
        "preferred_start": at(8, 11),
    }
    values.update(kw)
    return TrainingRequest(**values)  # type: ignore[arg-type]


def test_training_without_place_happens_at_the_residence_never_during_an_outing() -> None:
    # Two classes with a short gap: the user stays at the campus between them.
    classes = [
        Commitment("c1", "Analyse", at(8, 9), at(8, 11), CommitmentKind.CLASS, CAMPUS),
        Commitment("c2", "Algèbre", at(8, 12), at(8, 14), CommitmentKind.CLASS, CAMPUS),
    ]
    outcome = run_planning(
        week(commitments=classes, trainings=[_residence_training()]), FakeRouting()
    )
    (run,) = outcome.trainings
    day8 = [b for b in outcome.travel if b.start.day == 8]
    first_departure = min(b.start for b in day8)
    last_return = max(b.end for b in day8)
    span = Interval(run.start, run.start + timedelta(minutes=60))
    assert not span.overlaps(Interval(first_departure, last_return)), (run.start, day8)
    assert outcome.issues == []


def test_training_without_any_slot_at_the_residence_is_unplaced_not_forced() -> None:
    all_day = [Commitment("x", "Stage", at(8, 0), at(8, 23, 30), CommitmentKind.CLASS, CAMPUS)]
    request = _residence_training(earliest_start=at(8, 6), latest_end=at(8, 22))
    outcome = run_planning(week(commitments=all_day, trainings=[request]), FakeRouting())
    assert outcome.unplaced_trainings == ["run"]
    assert outcome.status is OutcomeStatus.PARTIAL
    assert any("aucun créneau libre à la résidence" in i for i in outcome.issues)
