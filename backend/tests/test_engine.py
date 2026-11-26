"""End-to-end planning pipeline with a fake MOTIS (journey durations per pair of places).

Each test names the validated decision it checks (ARCHITECTURE.md §7-8)."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tempo.planning.engine import (
    ActivityRequest,
    Commitment,
    CommitmentKind,
    OutcomeStatus,
    PlanningInput,
    PlanningOutcome,
    run_planning,
)
from tempo.scheduler.model import PlannedBlock, StudyDemand, TrainingPreferences
from tempo.timeutil import Interval, utc
from tempo.transport.location import Alternation, DeclaredStay
from tempo.transport.places import Coordinates, Place
from tempo.transport.routing import Journey, Leg, RoutingUnavailableError
from tempo.transport.travel import TravelStatus

TZ = ZoneInfo("Europe/Brussels")
HOME = Place("home", "Maman", Coordinates(50.668, 4.612))
DAD = Place("dad", "Papa", Coordinates(50.60, 4.70))
CAMPUS = Place("campus", "Campus", Coordinates(50.85, 4.35))
CLUB = Place("club", "Club", Coordinates(50.70, 4.60))
FRIEND = Place("friend", "Ami", Coordinates(50.50, 4.50))
PLACES = {p.coordinates: p.id for p in (HOME, DAD, CAMPUS, CLUB, FRIEND)}


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 12, day, hour, minute, tzinfo=TZ)


class FakeRouting:
    """Door-to-door minutes per pair of places (30 by default), any departure time."""

    def __init__(self, minutes: dict[frozenset[str], int] | None = None, fail: bool = False):
        self.minutes = minutes or {}
        self.fail = fail

    def plan(
        self, origin: Coordinates, destination: Coordinates, time: datetime, *, arrive_by: bool
    ) -> list[Journey]:
        if self.fail:
            raise RoutingUnavailableError("MOTIS down")
        pair = frozenset(
            {PLACES.get(origin, str(origin)), PLACES.get(destination, str(destination))}
        )
        trip = timedelta(minutes=self.minutes.get(pair, 30))
        start = time - trip if arrive_by else time
        leg = Leg("BUS", "a", "b", start, start + trip, start, start + trip, realtime=False)
        return [Journey((leg,), start, start + trip, 0, "fake")]


# Monday 7 December 2026; residences change every Friday (11, 18...) after the last class.
ALTERNATION = Alternation((HOME, DAD), date(2026, 12, 4), 7, TZ, switches={})


def lecture(day: int, start: int, end: int, title: str = "Analyse", **kw: Any) -> Commitment:
    return Commitment(
        f"class-{day}-{start}",
        title,
        at(day, start),
        at(day, end),
        CommitmentKind.CLASS,
        CAMPUS,
        **kw,
    )


def data(**overrides: Any) -> PlanningInput:
    values: dict[str, Any] = {
        "now": at(7, 6),
        "horizon_end": at(10, 23, 59),
        "timezone": TZ,
        "alternation": ALTERNATION,
        "commitments": [],
        "activities": [],
        "availability": [Interval(at(d, 8), at(d, 20)) for d in range(7, 11)],
        "demands": [],
        "training_preferences": TrainingPreferences(
            free_days=False, time_of_day=None, daylight=False
        ),
        "class_tolerance": timedelta(0),
        "solver_seconds": 5.0,
    }
    values.update(overrides)
    return PlanningInput(**values)


def trips(outcome: PlanningOutcome, day: int) -> list[tuple[str, str]]:
    return [
        (b.origin.id, b.destination.id)
        for b in sorted(outcome.travel, key=lambda b: utc(b.start))
        if b.start.astimezone(TZ).day == day
    ]


def check(outcome: PlanningOutcome) -> None:
    assert outcome.status not in (OutcomeStatus.INVALID, OutcomeStatus.NO_SOLUTION), (
        outcome.violations
    )


def test_day_is_one_outing_without_automatic_return_between_classes() -> None:
    """C19: direct journeys; never back home only because there is time."""
    outcome = run_planning(data(commitments=[lecture(8, 8, 10), lecture(8, 16, 18)]), FakeRouting())
    check(outcome)
    assert trips(outcome, 8) == [("home", "campus"), ("campus", "home")]
    assert outcome.status is OutcomeStatus.COMPLETE


def test_explicit_planned_location_between_classes_is_an_intermediate_return() -> None:
    """C19: an intermediate return only when the user asks (planned location)."""
    stay = DeclaredStay(HOME, at(8, 11), at(8, 15))
    outcome = run_planning(
        data(commitments=[lecture(8, 8, 10), lecture(8, 16, 18)], stays=[stay]), FakeRouting()
    )
    check(outcome)
    assert trips(outcome, 8) == [
        ("home", "campus"),
        ("campus", "home"),
        ("home", "campus"),
        ("campus", "home"),
    ]


def test_study_between_classes_happens_where_the_user_is() -> None:
    """C2: study availability has no place: between two classes the user studies on campus."""
    demand = StudyDemand("a", "Chimie", 180, at(8, 23, 59))
    outcome = run_planning(
        data(
            commitments=[lecture(8, 8, 10), lecture(8, 16, 18)],
            demands=[demand],
            availability=[Interval(at(8, 10), at(8, 16))],
            horizon_end=at(8, 23, 59),
        ),
        FakeRouting(),
    )
    check(outcome)
    assert outcome.result is not None
    assert sum(int(b.duration.total_seconds() // 60) for b in outcome.result.blocks) == 180


def test_training_at_the_residence_avoids_the_time_between_classes() -> None:
    """C1: avoided between two classes when another slot exists."""
    request = ActivityRequest(
        "t", "Renforcement", timedelta(minutes=60), at(8, 6), at(8, 22), training=True
    )
    outcome = run_planning(
        data(commitments=[lecture(8, 8, 10), lecture(8, 16, 18)], activities=[request]),
        FakeRouting(),
    )
    check(outcome)
    (placed,) = outcome.activities
    assert utc(placed.start) >= utc(at(8, 18, 30)) or utc(placed.end) <= utc(at(8, 7, 20))


def test_training_between_classes_is_allowed_when_nothing_else_fits() -> None:
    """C1 is a preference: the only slot is between the classes; the journeys home and back
    are planned for it (an explicit reason to go home), never an impossible plan."""
    request = ActivityRequest(
        "t", "Renforcement", timedelta(minutes=60), at(8, 11), at(8, 15), training=True
    )
    outcome = run_planning(
        data(commitments=[lecture(8, 8, 10), lecture(8, 16, 18)], activities=[request]),
        FakeRouting(),
    )
    check(outcome)
    (placed,) = outcome.activities
    assert at(8, 11) <= placed.start and placed.end <= at(8, 15)
    assert trips(outcome, 8) == [
        ("home", "campus"),
        ("campus", "home"),
        ("home", "campus"),
        ("campus", "home"),
    ]


def test_stay_elsewhere_excludes_residence_trainings_not_study() -> None:
    """C2: study follows the user; trainings at the residence do not happen at a friend's."""
    stay = DeclaredStay(FRIEND, at(8, 0), at(9, 0))
    request = ActivityRequest(
        "t", "Gainage", timedelta(minutes=60), at(8, 6), at(8, 22), training=True
    )
    outcome = run_planning(
        data(
            stays=[stay],
            activities=[request],
            demands=[StudyDemand("a", "a", 90, at(8, 23, 59))],
            horizon_end=at(8, 23, 59),
        ),
        FakeRouting(),
    )
    check(outcome)
    assert outcome.unplaced_activities == ["t"]
    assert outcome.result is not None and len(outcome.result.blocks) == 1


def test_temporary_stay_has_its_journeys_there_and_back() -> None:
    """Going to a planned location and coming back are mandatory journeys too."""
    stay = DeclaredStay(FRIEND, at(8, 10), at(9, 18))
    outcome = run_planning(data(stays=[stay]), FakeRouting())
    check(outcome)
    assert [(b.origin.id, b.destination.id) for b in outcome.travel] == [
        ("home", "friend"),
        ("friend", "home"),
    ]
    there, back = outcome.travel
    assert there.end == at(8, 10) and back.start == at(9, 18)
    stated = replace(ALTERNATION, switches={date(2026, 12, 11): at(11, 17)})
    moving = run_planning(data(alternation=stated, horizon_end=at(11, 23, 59)), FakeRouting())
    assert [(b.origin.id, b.destination.id) for b in moving.travel] == [("home", "dad")]


def test_exam_keeps_the_full_arrival_margin_classes_get_the_tolerance() -> None:
    """C4: tolerance for regular classes, never for exams; C18: official start shown."""
    exam = Commitment("exam", "Examen d'analyse", at(8, 9), at(8, 12), CommitmentKind.EXAM, CAMPUS)
    lesson = lecture(9, 9, 12)
    outcome = run_planning(
        data(commitments=[exam, lesson], class_tolerance=timedelta(minutes=10)), FakeRouting()
    )
    check(outcome)
    to_exam = next(b for b in outcome.travel if b.destination == CAMPUS and b.start.day == 8)
    to_class = next(b for b in outcome.travel if b.destination == CAMPUS and b.start.day == 9)
    assert to_exam.end == at(8, 9) and to_exam.journey is not None
    assert to_exam.journey.arrival == at(8, 8, 50)
    assert to_class.end == at(9, 9, 10) and to_class.event_start == at(9, 9)


def test_unknown_location_blocks_only_around_it_and_asks() -> None:
    """C5: plan what is safe, block only the uncertain period, ask the question."""
    odd = Commitment("x", "TP", at(9, 14), at(9, 16), CommitmentKind.CLASS, None, "Salle Z.99")
    outcome = run_planning(
        data(
            commitments=[odd, lecture(8, 10, 12)],
            demands=[StudyDemand("a", "a", 360, at(10, 23, 59))],
        ),
        FakeRouting(),
    )
    check(outcome)
    assert outcome.status is OutcomeStatus.PARTIAL
    assert any("Salle Z.99" in q for q in outcome.questions)
    assert outcome.result is not None
    blocks = [b.interval for b in outcome.result.blocks]
    assert sum(int(b.duration.total_seconds() // 60) for b in outcome.result.blocks) == 360
    assert not any(b.overlaps(Interval(at(9, 14), at(9, 16))) for b in blocks)
    assert trips(outcome, 8) == [("home", "campus"), ("campus", "home")]


def test_outage_keeps_previous_itineraries_and_the_plan_is_not_complete() -> None:
    """C6: preserved itineraries, marked not revalidated; never reported complete."""
    first = run_planning(data(commitments=[lecture(8, 10, 12)]), FakeRouting())
    kept = {
        (b.origin.id, b.destination.id, utc(b.end if not b.returning else b.start)): b.journey
        for b in first.travel
    }

    def previous(origin: Place, target: Place, when: datetime, outbound: bool) -> Journey | None:
        return kept.get((origin.id, target.id, utc(when)))

    down = run_planning(
        data(commitments=[lecture(8, 10, 12)], previous_travel=previous), FakeRouting(fail=True)
    )
    check(down)
    assert {b.status for b in down.travel} == {TravelStatus.STALE}
    assert down.status is OutcomeStatus.PARTIAL and down.unconfirmed_journeys == 2
    blind = run_planning(data(commitments=[lecture(8, 10, 12)]), FakeRouting(fail=True))
    assert {b.status for b in blind.travel} == {TravelStatus.UNKNOWN}
    assert blind.status is OutcomeStatus.PARTIAL


def test_real_itinerary_conflict_is_corrected_without_invalid_plan() -> None:
    """C8: the real journey (campus -> club, 80 min) moves the session to a feasible slot."""
    routing = FakeRouting({frozenset({"campus", "club"}): 80})
    request = ActivityRequest(
        "t",
        "Entraînement",
        timedelta(minutes=60),
        at(7, 17),
        at(7, 20),
        place=CLUB,
        preferred_start=at(7, 17, 30),
    )
    outcome = run_planning(
        data(commitments=[lecture(7, 14, 17)], activities=[request], horizon_end=at(7, 23, 59)),
        routing,
    )
    check(outcome)
    (placed,) = outcome.activities
    assert placed.start == at(7, 18, 45)
    assert all(b.status is TravelStatus.ROUTED for b in outcome.travel)
    assert outcome.status is OutcomeStatus.COMPLETE


def test_planned_slot_made_infeasible_by_the_itinerary_is_proposed_not_moved() -> None:
    """C8: a planned slot that the real journey no longer allows: alternative + approval."""
    routing = FakeRouting({frozenset({"campus", "club"}): 80})
    request = ActivityRequest(
        "t",
        "Entraînement",
        timedelta(minutes=60),
        at(8, 17),
        at(8, 21),
        place=CLUB,
        previous_start=at(8, 17, 30),
    )
    outcome = run_planning(data(commitments=[lecture(8, 14, 17)], activities=[request]), routing)
    check(outcome)
    (move,) = outcome.pending
    assert move.ref == "activity:t" and move.reason == "itinerary"
    assert move.to_start is not None and utc(move.to_start) >= utc(at(8, 18, 45))
    assert outcome.activities == [] and outcome.unplaced_activities == []
    assert outcome.status is OutcomeStatus.PENDING_APPROVAL


def test_today_items_stay_pinned_and_a_conflict_becomes_a_proposal() -> None:
    """C31: a new class on today's pinned training: proposal, never an automatic move."""
    pinned = Commitment(
        "activity:t", "Footing", at(7, 14), at(7, 15), CommitmentKind.TRAINING, CLUB, pinned=True
    )
    request = ActivityRequest(
        "t", "Footing", timedelta(minutes=60), at(7, 8), at(7, 22), place=CLUB, training=True
    )
    outcome = run_planning(
        data(
            commitments=[pinned, lecture(7, 13, 16)],
            activities=[request],
            horizon_end=at(7, 23, 59),
        ),
        FakeRouting(),
    )
    check(outcome)
    (move,) = outcome.pending
    assert move.reason == "same_day" and move.start == at(7, 14)
    assert outcome.activities == []
    free = run_planning(data(commitments=[pinned], horizon_end=at(7, 23, 59)), FakeRouting())
    assert free.pending == [] and any(b.destination == CLUB for b in free.travel)


def test_pinned_block_displaced_by_a_new_class_is_proposed() -> None:
    block = PlannedBlock("a", "maths", at(7, 14), at(7, 15, 30))
    outcome = run_planning(
        data(
            commitments=[lecture(7, 14, 16)],
            demands=[StudyDemand("a", "maths", 90, at(10, 23, 59))],
            pinned_blocks=[block],
        ),
        FakeRouting(),
    )
    check(outcome)
    (move,) = outcome.pending
    assert move.task_id == "a" and move.to_start is not None
    assert outcome.result is not None and outcome.result.blocks == []


def test_residence_changes_after_the_last_class_of_the_change_day() -> None:
    """C12: leave from the current residence, go to the next one after the last class."""
    switches = {date(2026, 12, 11): at(11, 16)}
    alternation = replace(ALTERNATION, switches=switches)
    outcome = run_planning(
        data(
            alternation=alternation,
            commitments=[lecture(11, 8, 10), lecture(11, 14, 16)],
            now=at(10, 20),
            horizon_end=at(11, 23, 59),
        ),
        FakeRouting(),
    )
    check(outcome)
    assert trips(outcome, 11) == [("home", "campus"), ("campus", "dad")]


def test_change_day_without_class_asks_and_blocks_only_that_day() -> None:
    """C12: no class on the change day: ask when; residence trainings wait that day only."""
    request = ActivityRequest(
        "t", "Gainage", timedelta(minutes=60), at(11, 6), at(12, 22), training=True
    )
    outcome = run_planning(
        data(activities=[request], now=at(10, 20), horizon_end=at(12, 23, 59)), FakeRouting()
    )
    check(outcome)
    assert any("changement de résidence" in q for q in outcome.questions)
    (placed,) = outcome.activities
    assert placed.start.astimezone(TZ).date() == date(2026, 12, 12)
    assert outcome.status is OutcomeStatus.PARTIAL


def test_realistic_week_is_valid_and_consistent() -> None:
    classes = [lecture(d, 10, 12) for d in (7, 8, 9, 10)]
    activities = [
        ActivityRequest(
            "t1",
            "Fractionné",
            timedelta(minutes=60),
            at(8, 6),
            at(9, 22),
            place=CLUB,
            training=True,
        ),
        ActivityRequest(
            "t2", "Footing", timedelta(minutes=45), at(9, 6), at(10, 22), training=True
        ),
    ]
    demands = [
        StudyDemand("m", "Maths", 360, at(10, 23, 59)),
        StudyDemand("p", "Physique", 270, None),
    ]
    outcome = run_planning(
        data(commitments=classes, activities=activities, demands=demands), FakeRouting()
    )
    check(outcome)
    assert outcome.status is OutcomeStatus.COMPLETE, (outcome.issues, outcome.unplanned_minutes)
    assert outcome.result is not None
    busy = [Interval(b.start, b.end) for b in outcome.travel] + [
        Interval(c.start, c.end) for c in classes
    ]
    busy += [a.reserved for a in outcome.activities]
    for block in outcome.result.blocks:
        assert not any(block.interval.overlaps(b) for b in busy)
    assert time(0) <= outcome.activities[0].start.astimezone(TZ).time()
