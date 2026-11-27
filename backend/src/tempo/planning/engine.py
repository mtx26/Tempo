"""Deterministic planning pipeline (ARCHITECTURE.md §7-8).

Pure function over plain inputs (no database, no clock), so it can be tested exhaustively and
re-run by the worker:

1. Fixed commitments (classes, exams, races, fixed, locked, under-way and pinned items) and
   their outings: base -> commitments (directly, never via a residence unless the user stated a
   planned location in between) -> base (``route_outings``). The same outings give the user's
   presence: away from the residence from the first departure to the last return.
2. Pinned items (the current day's existing placements, which only move with the user's
   approval) that no longer fit are displaced: an alternative is computed and proposed.
3. ONE CP-SAT model places study blocks and flexible activities together (no category first).
4. The journeys of the final plan are routed; a flexible activity whose real journey does not
   fit is re-placed with the real travel (at most ``MAX_CORRECTIONS`` times), or, when it was
   already planned there, displaced and proposed to the user.
5. The independent validator checks the WHOLE final plan; with violations nothing is
   published (``status = invalid``).

Status: ``complete`` only when all work and activities are placed, every mandatory journey is
confirmed by MOTIS and nothing waits for the user; ``partial`` when something is missing,
unconfirmed or asked (reported); ``pending_approval`` when only proposals are pending;
``no_solution`` when the solver gave no answer in its time (inconclusive, the previous plan
stays); ``invalid`` on violations.
"""

import functools
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from tempo.const import ACTIVITY_HOURS, BUFFER, MAX_CORRECTIONS, MAX_JOURNEY, MAX_TRAVEL
from tempo.scheduler.model import (
    ClassSlot,
    FlexibleActivity,
    PlacedActivity,
    PlannedBlock,
    PlanningProblem,
    PlanResult,
    PlanStatus,
    Preferences,
    StudyDemand,
    TemporaryPreference,
    TrainingPreferences,
    study_effort,
)
from tempo.scheduler.solver import solve
from tempo.scheduler.validate import (
    Leg,
    Occupied,
    PlacedSession,
    Stop,
    Trip,
    validate_journeys,
    validate_plan,
    validate_schedule,
)
from tempo.timeutil import Interval, subtract, utc
from tempo.transport.itinerary import CachedRouting, TripQuery, find_trips
from tempo.transport.location import Alternation, DeclaredStay, LocatedEvent, base_at
from tempo.transport.places import Place
from tempo.transport.routing import RoutingProvider, RoutingUnavailableError
from tempo.transport.travel import Previous, TravelBlock, TravelStatus, plan_day_travel


class CommitmentKind(StrEnum):
    CLASS = "class"
    EXAM = "exam"
    """An examination: no attendance tolerance, the full arrival margin."""
    RACE = "race"
    TRAINING = "training"
    OTHER = "other"


UNIVERSITY = frozenset({CommitmentKind.CLASS, CommitmentKind.EXAM})


@dataclass(frozen=True, slots=True)
class Commitment:
    """A fixed commitment (iCal class or exam, race, fixed, locked, under-way or pinned)."""

    ref: str
    title: str
    start: datetime
    end: datetime
    kind: CommitmentKind
    place: Place | None = None
    location_text: str | None = None
    movable: bool = False
    """A LOCKED flexible activity: it may move again, but only after the user's approval."""
    underway: bool = False
    """Already started: never moved, never the subject of a conflict proposal."""
    exact_times: bool = False
    """Times stated by the user for this occurrence (partial attendance): no tolerance."""
    pinned: bool = False
    """An existing placement of the current day: kept unless it no longer fits (then a move is
    proposed, never applied without the user's approval)."""
    at_home: bool = False
    """A training without place: at the residence in force."""


@dataclass(frozen=True, slots=True)
class ActivityRequest:
    """A flexible activity to place (training, personal outing, task session)."""

    activity_id: str
    title: str
    duration: timedelta
    earliest_start: datetime
    latest_end: datetime
    place: Place | None = None
    preferred_start: datetime | None = None
    previous_start: datetime | None = None
    preferred_day: date | None = None
    sequence_group: str | None = None
    sequence_index: int = 0
    rest_days: int = 0
    allowed: tuple[Interval, ...] | None = None
    """Category windows: the session must lie inside one of them."""
    blocked: tuple[Interval, ...] = ()
    """Unavailability (general or of this category)."""
    task_id: str | None = None
    deadline: datetime | None = None
    """End of the task's deadline day (never exceeded without approval)."""
    strict: bool = False
    training: bool = False
    outdoor: bool = True
    priority: int = 2

    @property
    def at_home(self) -> bool:
        """A training without place happens at the residence in force."""
        return self.training and self.place is None


@dataclass(frozen=True, slots=True)
class PendingMove:
    """A move waiting for the user's approval (displaced same-day item, or an activity whose
    real journey no longer fits its planned slot)."""

    ref: str
    """``activity:<id>`` or ``block:<id>``."""
    title: str
    task_id: str | None
    start: datetime
    end: datetime
    to_start: datetime | None
    to_end: datetime | None
    reason: str


@dataclass(frozen=True, slots=True)
class PlanningInput:
    now: datetime
    horizon_end: datetime
    timezone: ZoneInfo
    alternation: Alternation
    commitments: list[Commitment]
    activities: list[ActivityRequest]
    availability: list[Interval]
    """Study availability (time only), unavailability already removed."""
    demands: list[StudyDemand]
    fixed_blocks: list[PlannedBlock] = field(default_factory=list)
    """Locked and under-way blocks (always kept)."""
    pinned_blocks: list[PlannedBlock] = field(default_factory=list)
    """Existing blocks of the current day: kept unless they no longer fit (then proposed)."""
    previous_blocks: list[PlannedBlock] = field(default_factory=list)
    stays: list[DeclaredStay] = field(default_factory=list)
    reserved: list[Interval] = field(default_factory=list)
    """Target slots of pending move proposals (held until the user decides)."""
    preferences: Preferences = field(default_factory=Preferences)
    training_preferences: TrainingPreferences = field(default_factory=TrainingPreferences)
    temporary: list[TemporaryPreference] = field(default_factory=list)
    activity_hours: tuple[time, time] = ACTIVITY_HOURS
    buffer: timedelta = BUFFER
    class_tolerance: timedelta = timedelta(0)
    """The user may arrive this late at a class and leave it this early (never an exam)."""
    max_journey: timedelta = MAX_JOURNEY
    previous_travel: Previous | None = None
    """Itineraries of the published plan, reused (not revalidated) during a routing outage."""
    solver_seconds: float = 10.0
    expired_strict_tasks: dict[str, int] = field(default_factory=dict)
    """Task sessions whose strict deadline already passed with work left: task -> minutes."""


def _daylight(place: Place) -> Callable[[date], tuple[datetime, datetime]]:
    """Sunrise and sunset at the reference residence (both residences are close)."""
    from astral import Observer
    from astral.sun import sun

    observer = Observer(place.coordinates.lat, place.coordinates.lon)

    @functools.cache
    def at(day: date) -> tuple[datetime, datetime]:
        times = sun(observer, date=day, tzinfo=UTC)
        return times["sunrise"], times["sunset"]

    return at


class ConflictKind(StrEnum):
    LOCKED_BLOCK = "locked_block"
    LOCKED_ACTIVITY = "locked_activity"
    FIXED = "fixed"


@dataclass(frozen=True, slots=True)
class Conflict:
    """One overlap between protected items. ``subject`` is what may move after the user's
    approval (a locked block or activity); for FIXED it is simply the earlier commitment."""

    kind: ConflictKind
    subject_ref: str
    subject_title: str
    subject_start: datetime
    subject_kind: CommitmentKind | None
    other: Commitment

    def describe(self) -> str:
        what = "study block" if self.subject_kind is None else self.subject_kind.value
        return (
            f"{self.kind.value}: {what} '{self.subject_title}' "
            f"({self.subject_start.isoformat(timespec='minutes')}) overlaps "
            f"{self.other.kind.value} '{self.other.title}' "
            f"({self.other.start.isoformat(timespec='minutes')})"
        )


def find_conflicts(
    commitments: list[Commitment], locked_blocks: list[PlannedBlock], now: datetime
) -> list[Conflict]:
    """Every overlap between protected items (locked or fixed; pinned items are handled by
    displacement proposals). Detection only: publication is ``planning.service``'s job."""
    found: list[Conflict] = []
    items = sorted(
        (c for c in commitments if utc(c.end) > utc(now) and not c.pinned),
        key=lambda c: utc(c.start),
    )
    for i, first in enumerate(items):
        for second in items[i + 1 :]:
            if utc(second.start) >= utc(first.end):
                break
            movable = [c for c in (first, second) if c.movable and not c.underway]
            for subject in movable:
                other = second if subject is first else first
                found.append(
                    Conflict(
                        ConflictKind.LOCKED_ACTIVITY,
                        subject.ref,
                        subject.title,
                        subject.start,
                        subject.kind,
                        other,
                    )
                )
            if not movable:
                found.append(
                    Conflict(
                        ConflictKind.FIXED, first.ref, first.title, first.start, first.kind, second
                    )
                )
    for block in locked_blocks:
        if block.ref is None or utc(block.end) <= utc(now):
            continue
        for commitment in items:
            if block.interval.overlaps(Interval(commitment.start, commitment.end)):
                found.append(
                    Conflict(
                        ConflictKind.LOCKED_BLOCK,
                        block.ref,
                        block.subject,
                        block.start,
                        None,
                        commitment,
                    )
                )
    return found


class OutcomeStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    PENDING_APPROVAL = "pending_approval"
    NO_SOLUTION = "no_solution"
    INVALID = "invalid"
    """The validator rejected the plan: nothing may be published."""


@dataclass(slots=True)
class PlanningOutcome:
    status: OutcomeStatus
    result: PlanResult | None
    activities: list[PlacedActivity] = field(default_factory=list)
    unplaced_activities: list[str] = field(default_factory=list)
    travel: list[TravelBlock] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    deadline_capacity: dict[str, dict[str, Any]] = field(default_factory=dict)
    unplanned_minutes: dict[str, int] = field(default_factory=dict)
    hard_deadline_misses: list[str] = field(default_factory=list)
    deadline_misses: list[str] = field(default_factory=list)
    outside_hours: list[str] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    """Missing information to ask the user; only the affected items and periods wait."""
    question_due: dict[str, datetime] = field(default_factory=dict)
    pending: list[PendingMove] = field(default_factory=list)
    kept_blocks: list[PlannedBlock] = field(default_factory=list)
    """Pinned blocks kept as they are (carried into the new version)."""
    unconfirmed_journeys: int = 0


def free_minutes(
    availability: list[Interval],
    busy: list[Interval],
    until: datetime,
    since: datetime | None = None,
) -> int:
    """Study-window minutes before ``until`` not taken by busy time (an upper bound shared by
    every task: the diagnosis of a missed deadline)."""
    seconds = 0.0
    for window in sorted(availability, key=lambda w: utc(w.start)):
        start = utc(window.start) if since is None else max(utc(window.start), utc(since))
        end = min(utc(window.end), utc(until))
        if start < end:
            seconds += sum(
                (utc(p.end) - utc(p.start)).total_seconds()
                for p in subtract(Interval(start, end), busy)
            )
            since = end
    return int(seconds // 60)


def _presence(commitment: Commitment, tolerance: timedelta) -> LocatedEvent:
    """Where and when the user must be: a class allows arriving ``tolerance`` late and leaving
    ``tolerance`` early (never down to nothing); exams and other commitments are exact."""
    start, end = commitment.start, commitment.end
    if (
        commitment.kind is CommitmentKind.CLASS
        and not commitment.exact_times
        and end - start > 2 * tolerance
    ):
        start, end = start + tolerance, end - tolerance
    return LocatedEvent(
        start,
        end,
        commitment.place,
        commitment.ref,
        commitment.title,
        commitment.start,
        commitment.end,
    )


@dataclass(slots=True)
class Outings:
    """Journeys of the plan and the user's presence derived from them (one model)."""

    travel: list[TravelBlock] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    question_due: dict[str, datetime] = field(default_factory=dict)
    """When each answer is needed (urgency order of the questions)."""
    uncertain: list[Interval] = field(default_factory=list)
    """Periods around a commitment whose place or origin is unknown: nothing is planned."""
    away: list[Interval] = field(default_factory=list)
    """When the user cannot be at the residence at all: stays elsewhere, unknown periods.
    During an outing a training at the residence stays possible (the journeys home and back
    are then planned), only avoided between two classes (soft, C1)."""


def _stay_between(
    stays: list[DeclaredStay], previous: LocatedEvent, following: LocatedEvent
) -> Place | None:
    """An explicit intermediate presence: a planned location lying WITHIN the gap between two
    events (« entre midi et 14 h je rentre ») sends the user there in between. A planned
    location covering the events is only the base of the day (temporary base): it never
    implies a return between activities."""
    for stay in stays:
        if utc(stay.start) >= utc(previous.end) and utc(stay.end) <= utc(following.start):
            return stay.place
    return None


def route_outings(
    events: list[LocatedEvent], data: PlanningInput, routing: RoutingProvider
) -> Outings:
    """Mandatory journeys of every local day and the resulting presence.

    A day is ONE outing (no automatic return to a residence between activities), split only
    by an explicit planned location between two events or by an event of unknown place. The
    outing leaves from the base at its first event and returns to the base at its end (the
    base may change during the day: the residence change after the last class on a change
    day)."""
    tz, now = data.timezone, utc(data.now)
    out = Outings()

    def base(at: datetime) -> Place | None:
        return base_at(at, data.stays, data.alternation)

    def ask(when: datetime) -> None:
        local = when.astimezone(tz)
        question = (
            f"D'où partez-vous / où rentrez-vous le {local.strftime('%d/%m')} vers "
            f"{local.strftime('%H:%M')} ? (lieu prévu inconnu ou ambigu)"
        )
        if question not in out.questions:
            out.questions.append(question)
            out.question_due[question] = when

    days: dict[date, list[LocatedEvent]] = {}
    for event in events:
        days.setdefault(event.start.astimezone(tz).date(), []).append(event)
    sequence: list[tuple[list[LocatedEvent], Place | None, Place | None, bool, bool]] = []
    for day in sorted(days):
        ordered = sorted(days[day], key=lambda e: utc(e.start))
        current: list[LocatedEvent] = []
        start_base: Place | None = None
        start_known = True
        for event in ordered:
            if event.place is None:  # unknown place: breaks the outing, nothing guessed
                if current:
                    sequence.append((current, start_base, None, start_known, False))
                    previous_end = current[-1].end
                else:
                    previous_end = event.start - data.max_journey - data.buffer
                low = max(previous_end, event.start - data.max_journey - data.buffer, key=utc)
                if utc(low) < utc(event.start):
                    out.uncertain.append(Interval(low, event.start))
                out.uncertain.append(Interval(event.start, event.end + data.max_journey))
                current, start_known = [], False
                continue
            if current:
                between = _stay_between(data.stays, current[-1], event)
                if between is not None:
                    sequence.append((current, start_base, between, start_known, True))
                    current, start_base, start_known = [event], between, True
                    continue
            if not current and start_known:
                start_base = base(event.start)
            current.append(event)
        if current:
            last_event = max(current, key=lambda e: utc(e.end))
            end_moment = max(last_event.end, last_event.official_end or last_event.end, key=utc)
            sequence.append((current, start_base, base(end_moment), start_known, True))
    spans: list[Interval] = []
    for index, (outing, start_base, end_base, start_known, end_known) in enumerate(sequence):
        first = outing[0]
        last = max(outing, key=lambda e: utc(e.end))
        following = sequence[index + 1][0] if index + 1 < len(sequence) else None
        return_by = None
        if (
            following is not None
            and following[0].start.astimezone(tz).date() == last.end.astimezone(tz).date()
        ):
            return_by = following[0].start
        if start_known and start_base is None:
            out.issues.append(
                f"travel origin unknown at {first.start.isoformat(timespec='minutes')}"
            )
            ask(first.start)
            low = first.start - data.max_journey - data.buffer
            out.uncertain.append(Interval(low, first.start))
        if end_known and end_base is None:
            out.issues.append(f"return place unknown at {last.end.isoformat(timespec='minutes')}")
            ask(last.end)
            out.uncertain.append(Interval(last.end, last.end + data.max_journey))
        # Already under way: the departure check (worker) handles the current trip.
        start_place = None if utc(first.start) <= now or not start_known else start_base
        plan = plan_day_travel(
            outing,
            routing,
            buffer=data.buffer,
            start_place=start_place,
            end_place=end_base if end_known else None,
            return_by=return_by,
            max_journey=data.max_journey,
            previous=data.previous_travel,
        )
        out.travel += plan.blocks
        out.issues += plan.issues
        out.warnings += [w for w in plan.warnings if w not in out.warnings]
        spans.append(
            Interval(
                min([first.start, *(b.start for b in plan.blocks)], key=utc),
                max([last.end, *(b.end for b in plan.blocks)], key=utc),
            )
        )
    _base_changes(data, routing, spans, out)
    _presence_questions(data, events, out)
    residences = {p.id for p in data.alternation.residences}
    out.away += [Interval(s.start, s.end) for s in data.stays if s.place.id not in residences]
    out.away += out.uncertain
    for day in data.alternation.unknown_days(data.now, data.horizon_end):
        start = datetime.combine(day, time(0), tzinfo=tz)
        end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
        if any(utc(s.start) <= utc(start) and utc(end) <= utc(s.end) for s in data.stays):
            continue  # a planned location covers the whole day: its change time is irrelevant
        out.away.append(Interval(start, end))
        question = (
            f"Le {day.strftime('%d/%m')} est un jour de changement de résidence sans cours : "
            "à quelle heure changez-vous de résidence ?"
        )
        if question not in out.questions:
            out.questions.append(question)
            out.question_due[question] = start
    return out


def _presence_questions(data: PlanningInput, events: list[LocatedEvent], out: Outings) -> None:
    """Planned locations that may be an obligation rather than a base, and are not
    compatible with the day: asked, never decided.

    - A short planned location (within one day) overlapping a commitment elsewhere: is it
      only where the user leaves from, or must they stay there? Meanwhile it is a base.
    - An explicit presence between two activities that the journeys cannot honour."""
    tz = data.timezone
    for stay in data.stays:
        local_start, local_end = stay.start.astimezone(tz), stay.end.astimezone(tz)
        if local_start.date() != (local_end - timedelta(minutes=1)).date():
            continue  # several days: the base of those days
        clash = next(
            (
                e
                for e in events
                if e.place is not None
                and e.place != stay.place
                and Interval(e.start, e.end).overlaps(Interval(stay.start, stay.end))
            ),
            None,
        )
        unreachable = any(
            b.status is TravelStatus.IMPOSSIBLE
            and stay.place in (b.origin, b.destination)
            and Interval(b.start, max(b.end, b.start + timedelta(minutes=1), key=utc)).overlaps(
                Interval(stay.start - data.max_journey, stay.end + data.max_journey)
            )
            for b in out.travel
        )
        when = (
            f"le {local_start.strftime('%d/%m')} de {local_start.strftime('%H:%M')} à "
            f"{local_end.strftime('%H:%M')}"
        )
        if clash is not None:
            question = (
                f"Vous serez à {stay.place.name} {when}, pendant « {clash.title} » à "
                f"{clash.place.name if clash.place else '?'} : est-ce seulement votre point de "
                "départ, ou devez-vous rester à cet endroit ?"
            )
        elif unreachable:
            question = (
                f"Être à {stay.place.name} {when} n'est pas faisable avec les trajets entre vos "
                "activités : voulez-vous rester sur place, ou changer cette période ?"
            )
        else:
            continue
        if question not in out.questions:
            out.questions.append(question)
            out.question_due[question] = stay.start


def _base_changes(
    data: PlanningInput, routing: RoutingProvider, spans: list[Interval], out: Outings
) -> None:
    """Journeys of a change of base outside any outing: going to a planned location at its
    start (arriving by then), coming back at its end, moving to the next residence at a stated
    change time (leaving then). A change inside an outing is already its return."""
    now, end = utc(data.now), utc(data.horizon_end)
    moments: list[tuple[datetime, bool]] = []  # (instant, arrive by it?)
    for stay in data.stays:
        moments += [(stay.start, True), (stay.end, False)]
    alternation = data.alternation
    if len(alternation.residences) > 1:
        today = now.astimezone(data.timezone).date()
        index = (today - alternation.first_day).days // alternation.period_days
        while alternation.change_day(index) <= end.astimezone(data.timezone).date():
            moment = alternation.switch_moment(index)
            if moment is not None:
                moments.append((moment, False))
            index += 1
    for moment, arrive in sorted(moments, key=lambda m: utc(m[0])):
        if not now < utc(moment) < end or any(
            utc(s.start) <= utc(moment) <= utc(s.end) for s in spans
        ):
            continue
        before = base_at(moment - timedelta(minutes=1), data.stays, alternation)
        after = base_at(moment, data.stays, alternation)
        if before is None or after is None or before == after:
            continue  # unknown bases are asked elsewhere; no move, no journey
        title = f"Vers {after.name}"
        if arrive:
            target = LocatedEvent(moment, moment + timedelta(minutes=1), after, "", title)
            plan = plan_day_travel(
                [target],
                routing,
                buffer=timedelta(0),
                start_place=before,
                max_journey=data.max_journey,
                previous=data.previous_travel,
            )
        else:
            leave = LocatedEvent(moment - timedelta(minutes=1), moment, before, "", title)
            plan = plan_day_travel(
                [leave],
                routing,
                end_place=after,
                max_journey=data.max_journey,
                previous=data.previous_travel,
            )
        out.travel += plan.blocks
        out.issues += plan.issues
        out.warnings += [w for w in plan.warnings if w not in out.warnings]


def _estimate_travel(
    request: ActivityRequest, data: PlanningInput, routing: RoutingProvider, issues: list[str]
) -> tuple[timedelta, timedelta]:
    """Travel to reserve around a located flexible activity before it is placed: real journeys
    from the base around its preferred time (the final plan re-routes and corrects)."""
    if request.place is None:
        return timedelta(0), timedelta(0)
    anchor = (
        request.preferred_start
        or request.previous_start
        or max(request.earliest_start, data.now, key=utc)
    )
    home = base_at(anchor, data.stays, data.alternation)
    if home is None or home == request.place:
        return timedelta(0), timedelta(0)
    try:
        there = find_trips(
            routing,
            TripQuery(
                home.coordinates, request.place.coordinates, arrive_before=anchor, span=MAX_TRAVEL
            ),
        ).best
        back = find_trips(
            routing,
            TripQuery(
                request.place.coordinates,
                home.coordinates,
                depart_after=anchor + request.duration,
                span=MAX_TRAVEL,
            ),
        ).best
    except RoutingUnavailableError:
        return timedelta(0), timedelta(0)  # the final routing decides (stale or unplaced)
    if there is None or back is None:
        issues.append(f"activity '{request.title}': no journey to/from {request.place.name}")
    return (
        timedelta(0) if there is None else there.duration + data.buffer,
        timedelta(0) if back is None else back.duration,
    )


def _busy(blocks: list[TravelBlock]) -> list[Interval]:
    return [Interval(b.start, b.end) for b in blocks if utc(b.end) > utc(b.start)]


def _displaced(
    pinned: list[Commitment],
    others: list[Commitment],
    outings: Outings,
) -> list[Commitment]:
    """Pinned activities that no longer fit: overlap with another commitment, a journey or an
    uncertain period, or an infeasible journey of their own."""
    found: list[Commitment] = []
    for item in pinned:
        span = Interval(item.start, item.end)
        own = [b for b in outings.travel if item.ref in b.serves]
        clash = (
            any(span.overlaps(Interval(c.start, c.end)) for c in others if c.ref != item.ref)
            or any(
                span.overlaps(Interval(b.start, b.end))
                for b in outings.travel
                if item.ref not in b.serves and utc(b.end) > utc(b.start)
            )
            or any(span.overlaps(u) for u in outings.uncertain)
            or any(not b.status.feasible for b in own)
        )
        if clash:
            found.append(item)
    return found


def run_planning(data: PlanningInput, routing: RoutingProvider) -> PlanningOutcome:
    routing = CachedRouting(routing)  # one planning run: identical requests reach MOTIS once
    tz, now = data.timezone, utc(data.now)
    horizon = Interval(now, utc(data.horizon_end))
    issues: list[str] = []
    questions: list[str] = []
    due: dict[str, datetime] = {}

    fixed = [c for c in data.commitments if utc(c.end) > now and utc(c.start) < horizon.end]
    for c in fixed:
        if c.place is None and c.location_text:
            issues.append(
                f"unknown location '{c.location_text}' for '{c.title}': add a place alias "
                "(time blocked, travel not planned)"
            )
            question = (
                f"Où a lieu « {c.title} » (lieu indiqué : « {c.location_text} ») ? Donnez le "
                "lieu ou son adresse pour que je planifie le trajet."
            )
            questions.append(question)
            due[question] = c.start
    conflicts = find_conflicts(fixed, data.fixed_blocks, data.now)
    by_id = {r.activity_id: r for r in data.activities}

    def events_of(items: list[Commitment]) -> list[LocatedEvent]:
        located: list[LocatedEvent] = []
        for c in items:
            if c.place is not None or c.location_text:
                located.append(_presence(c, data.class_tolerance))
            elif c.at_home:
                where = base_at(c.start, data.stays, data.alternation)
                if where is not None:
                    located.append(LocatedEvent(c.start, c.end, where, c.ref, c.title, c.start))
        return located

    # 1-2. Fixed outings; pinned items that no longer fit are displaced (approval needed).
    pending: list[PendingMove] = []
    outings = route_outings(events_of(fixed), data, routing)
    pinned = [c for c in fixed if c.pinned]
    displaced = _displaced(pinned, [c for c in fixed if not c.pinned], outings)
    if displaced:
        gone = {c.ref for c in displaced}
        fixed = [c for c in fixed if c.ref not in gone]
        outings = route_outings(events_of(fixed), data, routing)
    fixed_busy = [Interval(c.start, c.end) for c in fixed]
    # A return whose journey is unknown before the activities are placed is provisional:
    # the final routing decides it (and re-plans around it when it touches something).
    provisional = [
        b for b in outings.travel if not (b.returning and b.status is TravelStatus.UNKNOWN)
    ]
    base_busy = fixed_busy + _busy(provisional) + outings.uncertain + list(data.reserved)
    kept_blocks: list[PlannedBlock] = []
    displaced_blocks: list[PlannedBlock] = []
    for block in data.pinned_blocks:
        if any(block.interval.overlaps(b) for b in base_busy):
            displaced_blocks.append(block)
        else:
            kept_blocks.append(replace(block, locked=True))

    # Pinned activities are commitments; their request is solved only when displaced.
    pinned_refs = {c.ref for c in pinned}
    requests = [
        r
        for r in data.activities
        if utc(r.latest_end) > now and f"activity:{r.activity_id}" not in pinned_refs
    ]
    for c in displaced:
        request = by_id.get(c.ref.removeprefix("activity:"))
        if request is not None:
            requests.append(replace(request, previous_start=None))
    displaced_ids = {c.ref.removeprefix("activity:") for c in displaced}
    issues_travel: list[str] = []
    estimates = {r.activity_id: _estimate_travel(r, data, routing, issues_travel) for r in requests}
    corrections: dict[str, tuple[timedelta, timedelta]] = {}
    itinerary_moves: set[str] = set()
    sun = _daylight(data.alternation.residences[0])
    classes = [ClassSlot(c.start, c.end, c.title) for c in fixed if c.kind in UNIVERSITY]
    sport_days = [
        c.start.astimezone(tz).date()
        for c in fixed
        if c.kind in (CommitmentKind.TRAINING, CommitmentKind.RACE)
    ]

    def flexible(request: ActivityRequest) -> FlexibleActivity:
        before, after = estimates[request.activity_id]
        blocked = list(request.blocked)
        if request.activity_id in corrections:
            c_before, c_after = corrections[request.activity_id]
            before, after = max(before, c_before), max(after, c_after)
        windows = list(request.allowed or (Interval(request.earliest_start, request.latest_end),))
        removed = blocked + (outings.away if request.at_home else outings.uncertain)
        allowed = tuple(p for w in windows for p in subtract(w, removed))
        latest = request.latest_end
        if request.deadline is not None:
            latest = min(latest, request.deadline, key=utc)
        return FlexibleActivity(
            activity_id=request.activity_id,
            title=request.title,
            duration=request.duration,
            earliest_start=request.earliest_start,
            latest_end=latest if utc(latest) > utc(request.earliest_start) else request.latest_end,
            preferred_start=request.preferred_start,
            previous_start=request.previous_start,
            travel_before=before,
            travel_after=after,
            preferred_day=request.preferred_day,
            sequence_group=request.sequence_group,
            sequence_index=request.sequence_index,
            rest_days=request.rest_days,
            allowed=allowed,
            training=request.training,
            outdoor=request.outdoor,
            priority=request.priority,
            deadline=request.deadline is not None,
        )

    # Displaced blocks keep their effort in the solve: the block found for it is the
    # alternative proposed to the user (removed from the published plan below).
    demands = list(data.demands)

    attempt = 0
    while True:
        problem = PlanningProblem(
            horizon=horizon,
            timezone=tz,
            availability=[
                Interval(max(w.start, data.now, key=utc), w.end)
                for w in data.availability
                if utc(w.end) > now and utc(max(w.start, data.now, key=utc)) < utc(w.end)
            ],
            busy=base_busy,
            demands=demands,
            activities=[flexible(r) for r in requests],
            fixed_blocks=[b for b in data.fixed_blocks if utc(b.end) > now] + kept_blocks,
            preferences=data.preferences,
            previous_blocks=[b for b in data.previous_blocks if utc(b.start) >= now],
            training=data.training_preferences,
            temporary=data.temporary,
            activity_hours=data.activity_hours,
            classes=classes,
            sport_days=sport_days,
            sun=sun,
        )
        result = solve(
            problem,
            time_limit_seconds=data.solver_seconds if attempt == 0 else data.solver_seconds / 2,
        )
        if result.status is PlanStatus.NO_SOLUTION:
            issues.append(
                f"no answer within the solver time limit ({result.solver_status}): "
                "inconclusive, not a proof of impossibility; the previous plan is kept"
            )
            return PlanningOutcome(
                OutcomeStatus.NO_SOLUTION,
                None,
                issues=issues,
                conflicts=conflicts,
                questions=questions + outings.questions,
                question_due={**due, **outings.question_due},
            )
        # 4. Final journeys with the placed activities, then real-travel corrections.
        placed = {p.activity_id: p for p in result.activities}
        flexible_events = [
            LocatedEvent(
                p.start,
                p.end,
                where,
                f"activity:{p.activity_id}",
                by_id[p.activity_id].title,
                p.start,
            )
            for p in result.activities
            if (
                where := by_id[p.activity_id].place
                or (
                    base_at(p.start, data.stays, data.alternation)
                    if by_id[p.activity_id].at_home
                    else None
                )
            )
            is not None
        ]
        final = route_outings(events_of(fixed) + flexible_events, data, routing)
        clashes = _travel_clashes(
            result, final, [_presence(c, data.class_tolerance) for c in fixed], by_id
        )
        extra = _uncovered_travel(result, final, base_busy)
        if (not clashes and not extra) or attempt >= MAX_CORRECTIONS:
            break
        attempt += 1
        base_busy = base_busy + extra
        for activity_id in clashes:
            spot = placed[activity_id]
            request = by_id[activity_id]
            if request.previous_start is not None and utc(request.previous_start) == utc(
                spot.start
            ):
                itinerary_moves.add(activity_id)  # its planned slot no longer works
            own = [b for b in final.travel if f"activity:{activity_id}" in b.serves]
            before = max(
                [utc(spot.start) - utc(b.start) for b in own if utc(b.end) <= utc(spot.start)],
                default=timedelta(0),
            )
            # Travel actually needed after it (arrival of the journey, not the wait before
            # the next commitment).
            after = max(
                [
                    utc(b.journey.arrival if b.journey is not None else b.end) - utc(spot.end)
                    for b in own
                    if utc(b.start) >= utc(spot.end)
                ],
                default=timedelta(0),
            )
            # Reserve the real travel next time (it only grows: no oscillation).
            previous = corrections.get(activity_id, (timedelta(0), timedelta(0)))
            corrections[activity_id] = (max(previous[0], before), max(previous[1], after))
    # Unresolved clashes after the bounded corrections: left unplaced, reported.
    for activity_id in clashes:
        issues.append(
            f"« {by_id[activity_id].title} » : aucun créneau compatible avec les trajets réels "
            "trouvé ; séance non placée"
        )
    clashing = set(clashes)
    activities = [p for p in result.activities if p.activity_id not in clashing]
    unplaced = [*result.unplaced, *clashing]

    # Displaced / itinerary-incompatible items: their alternative waits for approval.
    for spot in result.activities:
        request = by_id[spot.activity_id]
        before, after = estimates[spot.activity_id]
        if request.activity_id in corrections:
            before = max(before, corrections[request.activity_id][0])
            after = max(after, corrections[request.activity_id][1])
        planned_at = request.previous_start
        if planned_at is None or utc(planned_at) == utc(spot.start) or utc(planned_at) <= now:
            continue
        session = Interval(planned_at, planned_at + request.duration)
        reserved = Interval(planned_at - before, planned_at + request.duration + after)
        # Its planned slot is free, only the real journey no longer fits: approval (C8).
        if not any(session.overlaps(b) for b in base_busy) and any(
            reserved.overlaps(b) for b in base_busy
        ):
            itinerary_moves.add(spot.activity_id)
    pending_ids = displaced_ids | (itinerary_moves - clashing)
    for activity_id in sorted(pending_ids):
        request = by_id[activity_id]
        alternative = next((p for p in activities if p.activity_id == activity_id), None)
        origin = next((c for c in displaced if c.ref == f"activity:{activity_id}"), None)
        start = origin.start if origin is not None else request.previous_start
        assert start is not None  # noqa: S101 - pinned or previously placed
        pending.append(
            PendingMove(
                f"activity:{activity_id}",
                request.title,
                request.task_id,
                start,
                start + request.duration,
                None if alternative is None else alternative.start,
                None if alternative is None else alternative.end,
                "same_day" if origin is not None else "itinerary",
            )
        )
    activities = [p for p in activities if p.activity_id not in pending_ids]
    unplaced = [a for a in unplaced if a not in pending_ids]
    blocks = list(result.blocks)
    for block in displaced_blocks:
        replacement = next(
            (
                b
                for b in blocks
                if b.task_id == block.task_id
                and b not in problem.fixed_blocks
                and b.duration == block.duration
                and b not in data.previous_blocks
            ),
            None,
        )
        if replacement is not None:
            blocks.remove(replacement)
        pending.append(
            PendingMove(
                block.ref or f"block-at:{block.task_id}:{utc(block.start).isoformat()}",
                block.subject,
                block.task_id,
                block.start,
                block.end,
                None if replacement is None else replacement.start,
                None if replacement is None else replacement.end,
                "same_day",
            )
        )
    result = replace(
        result,
        blocks=blocks,
        study_effort=study_effort(problem.demands, blocks)
        if blocks != result.blocks
        else result.study_effort,
        activities=activities,
        unplaced=unplaced,
        # Work held for a pending proposal is not in the published plan: not complete.
        status=PlanStatus.PARTIAL if pending else result.status,
    )
    if pending or clashing:  # the published plan routes only what is really placed
        kept_ids = {p.activity_id for p in activities}
        final = route_outings(
            events_of(fixed)
            + [e for e in flexible_events if e.ref.removeprefix("activity:") in kept_ids],
            data,
            routing,
        )

    outcome = PlanningOutcome(
        status=OutcomeStatus.COMPLETE,
        result=result,
        activities=activities,
        unplaced_activities=unplaced,
        travel=final.travel,
        issues=issues + final.issues + issues_travel,
        warnings=final.warnings,
        unplanned_minutes=dict(result.unplanned_minutes),
        hard_deadline_misses=list(result.hard_deadline_misses),
        deadline_misses=list(result.deadline_misses),
        outside_hours=result.outside_hours,
        conflicts=conflicts,
        questions=questions + [q for q in final.questions if q not in questions],
        question_due={**due, **final.question_due},
        pending=pending,
        kept_blocks=kept_blocks,
    )
    for missing in unplaced:
        request = by_id[missing]
        at_home = " à la résidence" if request.at_home else ""
        outcome.issues.append(
            f"« {request.title} » : aucun créneau libre{at_home} dans sa période sans enfreindre "
            "les contraintes (trajets compris) ; à replanifier avec l'utilisateur (autre jour, "
            "période plus large ou séance plus courte)"
        )
    _task_sessions(data, outcome)
    _deadline_capacity(data, problem, outcome)
    outcome.violations = _validate(data, problem, result, fixed, final, by_id)
    outcome.unconfirmed_journeys = sum(
        1 for b in final.travel if b.status is not TravelStatus.ROUTED
    )
    if outcome.violations:
        outcome.status = OutcomeStatus.INVALID
    elif (
        outcome.unplanned_minutes
        or outcome.deadline_misses
        or outcome.unplaced_activities
        or outcome.unconfirmed_journeys
        or outcome.questions
    ):
        outcome.status = OutcomeStatus.PARTIAL
    elif outcome.pending:
        outcome.status = OutcomeStatus.PENDING_APPROVAL
    return outcome


def _uncovered_travel(result: PlanResult, final: Outings, busy: list[Interval]) -> list[Interval]:
    """Final journeys (not those of a placed activity) that the solve did not treat as busy
    and that touch a placed study block or activity: to reserve in the next solve."""
    served = {f"activity:{p.activity_id}" for p in result.activities}
    placed = [b.interval for b in result.blocks] + [a.reserved for a in result.activities]
    found: list[Interval] = []
    for block in final.travel:
        if utc(block.end) <= utc(block.start) or served & set(block.serves):
            continue
        span = Interval(block.start, block.end)
        if any(span.overlaps(p) for p in placed) and not any(b.contains(span) for b in busy):
            found.append(span)
    return found


def _travel_clashes(
    result: PlanResult,
    final: Outings,
    attended: list[LocatedEvent],
    by_id: dict[str, ActivityRequest],
) -> list[str]:
    """Placed activities whose REAL journeys (final plan) are infeasible or overlap
    something: commitments, uncertain periods, study blocks or other activities."""
    clashes: list[str] = []
    # Journeys reach commitments at the attended times (class tolerance included).
    taken = [Interval(e.start, e.end) for e in attended] + [b.interval for b in result.blocks]
    sessions = {p.activity_id: Interval(p.start, p.end) for p in result.activities}
    for activity_id, span in sessions.items():
        ref = f"activity:{activity_id}"
        own = [b for b in final.travel if ref in b.serves]
        trips = [Interval(b.start, b.end) for b in own if utc(b.end) > utc(b.start)]
        others = [s for a, s in sessions.items() if a != activity_id]
        other_trips = [
            Interval(b.start, b.end)
            for b in final.travel
            if ref not in b.serves and utc(b.end) > utc(b.start)
        ]
        if (
            any(not b.status.feasible for b in own)
            or any(t.overlaps(x) for t in trips for x in [*taken, *others, *final.uncertain])
            or any(span.overlaps(x) for x in [*other_trips, *final.uncertain])
        ) or (by_id[activity_id].at_home and any(span.overlaps(a) for a in final.uncertain)):
            clashes.append(activity_id)
    return clashes


def _validate(
    data: PlanningInput,
    problem: PlanningProblem,
    result: PlanResult,
    fixed: list[Commitment],
    final: Outings,
    by_id: dict[str, ActivityRequest],
) -> list[str]:
    tz = data.timezone
    general = data.training_preferences.rest_days
    sessions = [
        PlacedSession(
            ref=f"activity:{p.activity_id}",
            title=by_id[p.activity_id].title,
            start=p.start,
            end=p.end,
            earliest_start=by_id[p.activity_id].earliest_start,
            latest_end=by_id[p.activity_id].latest_end,
            allowed=None
            if by_id[p.activity_id].allowed is None
            else tuple(
                part
                for w in by_id[p.activity_id].allowed or ()
                for part in subtract(w, by_id[p.activity_id].blocked)
            ),
            deadline=by_id[p.activity_id].deadline,
            sequence_group=by_id[p.activity_id].sequence_group,
            sequence_index=by_id[p.activity_id].sequence_index,
            training=by_id[p.activity_id].training,
            rest_days=max(general, by_id[p.activity_id].rest_days)
            if by_id[p.activity_id].training
            else 0,
        )
        for p in result.activities
    ]
    occupied = [
        Occupied(c.title, Interval(e.start, e.end))
        for c in fixed
        if utc((e := _presence(c, data.class_tolerance)).end) > utc(e.start)
    ]
    placed_refs = {f"activity:{p.activity_id}" for p in result.activities}
    trips = [
        Trip(
            f"{b.origin.name} -> {b.destination.name}",
            Interval(b.start, b.end),
            b.status.feasible,
            next((r for r in b.serves if r in placed_refs), None),
        )
        for b in final.travel
        if utc(b.end) > utc(b.start)
    ]
    # Blocks the planner placed (locked ones in conflict are proposals, not violations).
    study = [
        Occupied(f"study {b.subject}", b.interval)
        for b in result.blocks
        if b not in problem.fixed_blocks
    ]
    at_home = frozenset(f"activity:{a}" for a, r in by_id.items() if r.at_home)
    residences = {p.id for p in data.alternation.residences}
    away = [Interval(s.start, s.end) for s in data.stays if s.place.id not in residences]
    away += final.uncertain
    sport_days = [
        c.start for c in fixed if c.kind in (CommitmentKind.TRAINING, CommitmentKind.RACE)
    ]
    # The day as the user lives it: every located commitment (official times; a class lets a
    # journey use its tolerance) and every placed located activity, and the real journeys.
    stops = [
        Stop(
            c.title,
            c.place.id,
            Interval(c.start, c.end),
            data.class_tolerance
            if c.kind is CommitmentKind.CLASS and not c.exact_times
            else timedelta(0),
        )
        for c in fixed
        if c.place is not None
    ] + [
        Stop(by_id[p.activity_id].title, where.id, Interval(p.start, p.end))
        for p in result.activities
        if (
            where := by_id[p.activity_id].place
            or (
                base_at(p.start, data.stays, data.alternation)
                if by_id[p.activity_id].at_home
                else None
            )
        )
        is not None
    ]
    legs = [
        Leg(
            f"{b.origin.name} -> {b.destination.name}",
            b.origin.id,
            b.destination.id,
            Interval(b.start, b.end),
            b.status.feasible,
        )
        for b in final.travel
        if utc(b.end) > utc(b.start)
    ]
    return (
        validate_plan(problem, result)
        + validate_schedule(
            sessions,
            occupied,
            trips,
            tz,
            study=study,
            away=away,
            at_home=at_home,
            sport_days=sport_days,
        )
        + validate_journeys(legs, stops, tz)
    )


def _task_sessions(data: PlanningInput, outcome: PlanningOutcome) -> None:
    """Task sessions (non-splittable or located tasks) reported like study tasks."""
    placed = {p.activity_id for p in outcome.activities}
    pending = {p.ref.removeprefix("activity:") for p in outcome.pending}
    for request in data.activities:
        if request.task_id is None or utc(request.latest_end) <= utc(data.now):
            continue
        if request.activity_id in placed or request.activity_id in pending:
            continue
        minutes = int(request.duration.total_seconds() // 60)
        outcome.unplanned_minutes[request.task_id] = (
            outcome.unplanned_minutes.get(request.task_id, 0) + minutes
        )
        if request.deadline is not None and request.task_id not in outcome.deadline_misses:
            outcome.deadline_misses.append(request.task_id)
        if request.strict and request.task_id not in outcome.hard_deadline_misses:
            outcome.hard_deadline_misses.append(request.task_id)
    for task_id, minutes in data.expired_strict_tasks.items():
        outcome.unplanned_minutes[task_id] = minutes
        for misses in (outcome.hard_deadline_misses, outcome.deadline_misses):
            if task_id not in misses:
                misses.append(task_id)
        outcome.issues.append(f"task {task_id}: strict deadline already passed with work left")


def _deadline_capacity(
    data: PlanningInput, problem: PlanningProblem, outcome: PlanningOutcome
) -> None:
    by_task = {d.task_id: d for d in data.demands}
    fixed = problem.busy + [b.interval for b in problem.fixed_blocks]
    taken = fixed + [a.reserved for a in outcome.activities]
    for task_id in outcome.deadline_misses:
        demand = by_task.get(task_id)
        if demand is None or demand.target is None:
            continue
        since = max(problem.horizon.start, demand.not_before or problem.horizon.start, key=utc)
        until = min(problem.horizon.end, demand.target, key=utc)
        fixed_minutes = sum(
            max(0, int((min(utc(b.end), utc(demand.target)) - utc(b.start)).total_seconds() // 60))
            for b in problem.fixed_blocks
            if b.task_id == task_id
        )
        free = free_minutes(problem.availability, taken, until, since)
        upper_bound = free_minutes(problem.availability, fixed, until, since) + fixed_minutes
        outcome.deadline_capacity[task_id] = {
            "deadline": (demand.target - timedelta(minutes=1))
            .astimezone(data.timezone)
            .date()
            .isoformat(),
            "deadline_end": demand.target.isoformat(),
            "strict": task_id in outcome.hard_deadline_misses,
            "needed_minutes": demand.remaining_minutes,
            "free_minutes_before_deadline": free,
            "fixed_minutes_before_deadline": fixed_minutes,
            "minimum_missing_minutes": max(0, demand.remaining_minutes - upper_bound),
            "capacity_upper_bound_minutes": upper_bound,
            "uncovered_at_deadline_minutes": max(
                0,
                demand.remaining_minutes
                - (outcome.result.study_effort[task_id].before_deadline_minutes or 0),
            )
            if outcome.result is not None and task_id in outcome.result.study_effort
            else demand.remaining_minutes,
        }
