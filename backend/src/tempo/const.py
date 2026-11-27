"""Application constants and configurable defaults, in one place (ARCHITECTURE.md §8).

Bottom of the layer stack: no ``tempo`` import, so every package may depend on it.

Rules:
- A value the user can change in the conversation is a SETTING (``app_setting``); its default
  lives in ``SETTING_DEFAULTS`` and is read through ``db.store.get_setting``. The database value
  always wins.
- Dynamic values (sunrise and sunset, journey durations, positions) are never constants.
- Technical details strictly local to one implementation (parsing limits of a file format,
  API paths, regular expressions, prompts) stay in their module.
"""

from datetime import time, timedelta
from pathlib import Path

# --- configurable defaults (settings) ---------------------------------------------------------

HORIZON_DAYS = 14
"""Rolling detailed planning horizon (setting ``horizon_days``, 1-42)."""
BUFFER_MINUTES = 10
"""Arrive this long before a commitment (setting ``buffer_minutes``); exams included."""
CLASS_TOLERANCE_MINUTES = 10
"""Regular classes only: the most the user accepts to arrive late / leave early (setting,
never for an exam); a soft allowance, see ``TOLERANCE_PER_MINUTE``."""
TOLERANCE_PER_MINUTE = 6
"""Cost of each tolerance minute used (late arrival or early departure at a class), in minutes
of travel and waiting: 10 tolerance minutes are used only to save more than an hour."""
DEPARTURE_REMINDER_MINUTES = (60, 15)
"""Departure reminders before the advised leave time (setting ``reminder_minutes``)."""
MAX_JOURNEY_MINUTES = 180
"""Longest acceptable door-to-door journey (setting ``max_journey_minutes``)."""
ACTIVITY_HOURS = (time(6, 0), time(22, 0))
"""Preferred hours of flexible activities (soft; setting ``activity_hours``)."""

SETTING_DEFAULTS: dict[str, object] = {
    "horizon_days": HORIZON_DAYS,
    "buffer_minutes": BUFFER_MINUTES,
    "class_tolerance_minutes": CLASS_TOLERANCE_MINUTES,
    "reminder_minutes": list(DEPARTURE_REMINDER_MINUTES),
    "max_journey_minutes": MAX_JOURNEY_MINUTES,
    "activity_hours": {
        "start": ACTIVITY_HOURS[0].isoformat("minutes"),
        "end": ACTIVITY_HOURS[1].isoformat("minutes"),
    },
}

# Setting keys (stored in ``app_setting``).
ALTERNATION_SETTING = "residence_alternation"
SWITCHES_SETTING = "residence_switches"
"""Change times stated by the user for given dates (``{"YYYY-MM-DD": "HH:MM"}``)."""
ACCESS_OVERRIDES_SETTING = "access_overrides"
"""Temporary transport access points for a period (never the persistent default)."""
CLASS_ATTENDANCE_SETTING = "class_attendance"
"""Per class occurrence (``event:<id>``): ``{"absent": true}`` or the stated ``arrive_at`` /
``leave_at`` (ISO); absent key = the whole class is attended."""
TEMPORARY_SETTING = "temporary_preferences"
"""Preferences that hold for a period then expire: ``[{start, end, key, value}]``."""
DECLINED_SETTING = "declined_today"
"""Items whose same-day move the user declined: ``{ref: date}`` (not re-placed that day)."""
MAX_JOURNEY_SETTING = "max_journey_minutes"
LLM_MODEL_SETTING = "llm_model"

# --- study and tasks ----------------------------------------------------------------------------

SESSION_MINUTES = 90
"""Preferred study / task session (shorter allowed when necessary)."""
MIN_SESSION_MINUTES = 30
GRID_MINUTES = 15
"""Planning grid."""
BREAK_MINUTES = 15
"""Preferred break between two study sessions (consecutive sessions stay allowed)."""
SAME_SUBJECT_GAP_MINUTES = 60
"""Same subject again within this gap (or right after its class) is avoided: alternation."""
TASK_SESSION_HORIZON = timedelta(days=14)
"""Sessions of a located / non-splittable task are created for this period."""
SERIES_HORIZON = timedelta(days=60)
"""Recurring personal events are materialised this far ahead."""
MAX_SERIES_OCCURRENCES = 400
EXAM_WORDS = frozenset(
    {"examen", "examens", "exam", "exams", "partiel", "partiels", "interrogation", "interro"}
)
"""Words of an iCal title that make it an examination (no attendance tolerance)."""
EXAM_DOUBT_WORDS = frozenset(
    {
        "test",
        "tests",
        "evaluation",
        "evaluations",
        "controle",
        "epreuve",
        "epreuves",
        "qcm",
        "oral",
        "orals",
        "defense",
        "quiz",
        "certificatif",
    }
)
"""Words that may mean an examination: the user is asked; meanwhile the full margin is kept."""
EXAM_SETTING = "exam_answers"
"""The user's answers per iCal series (UID): examination or not."""
POSTPONE_STEPS_DAYS = (2, 7, 14, 28)
"""Postponements simulated by ``propose_alternatives`` (smallest that resolves)."""
MAX_ALTERNATIVE_TASKS = 3

# --- planner (CP-SAT) ---------------------------------------------------------------------------

SOLVER_SECONDS = 10.0
"""Time limit of one solve (a correction re-solve gets half)."""
MAX_CORRECTIONS = 2
"""Re-placements with the real travel of the final plan (bounded)."""
MAX_BLOCKS_PER_TASK_DAY = 6
"""Model size bound only (not a preference): candidate sessions per task and day."""

# Objective weights. Leaving work unplanned dominates every preference an item can accumulate
# (tests/test_scheduler_weights.py), so a preference never makes something unplanned.
UNPLANNED_PER_MINUTE = 5_000
"""x priority (1-3): work with a deadline and every flexible activity, whatever its category."""
NO_DEADLINE_PER_MINUTE = 1_250
"""x priority: a task without deadline only uses the remaining availability (even at priority 3
it never displaces work with a real deadline at priority 1)."""
OUTSIDE_HOURS_COST = 40_000
DARK_COST = 30_000
"""Outdoor training before sunrise or after sunset: strongly avoided, never forbidden."""
BETWEEN_CLASSES_COST = 10_000
"""Training between two classes of the same day."""
CLASS_DAY_COST = 8_000
TIME_OF_DAY_STEP_COST = 1_000
TIME_OF_DAY_FAR_COST = 3_000
"""Trainings, preferred time of day (morning by default; afternoon or evening when asked):
the next period costs the step, the opposite one the far cost (morning: afternoon, then
evening last)."""
TIMES_OF_DAY = ("morning", "afternoon", "evening")
"""Periods by the middle of the session: before 12:00, before 18:00, from 18:00."""
COACH_DAY_COST = 500
"""Per day away from the coach's suggested day."""
PREFERRED_START_COST = 15_000
"""Away from a time the user (or coach) gave for THIS activity: above the general day / time
preferences (at most 11 000 together), below the dark (an explicit time is kept unless dark)."""
PREFERRED_START_PER_MINUTE = 10
STABILITY_COST = 4_000
"""Moving an item of the published plan costs this / (days ahead + 1) (tomorrow 2 000, in a
week ~500), plus a shift cost reaching the same amount after 4 hours."""
DISTRIBUTION_PER_MINUTE = 5
"""Study minutes of a task above its regular daily share until the deadline."""
SAME_SUBJECT_COST = 400
MISSING_BREAK_COST = 150
SHORT_BLOCK_COST = 100
OVERSHOOT_PER_MINUTE = 2

# --- transport and presence ---------------------------------------------------------------------

MAX_TRAVEL = timedelta(hours=6)
"""Search span of one journey request; bound of a return without known next commitment."""
SEARCH_SPAN = timedelta(hours=2)
"""Default window around a single journey bound."""
MAX_POSITION_AGE = timedelta(minutes=10)
MAX_POSITION_ACCURACY_M = 200.0
"""A phone position is trusted only when this fresh and this precise."""
ARRIVED_RADIUS_M = 150.0
LEAVE_NOW_WINDOW = timedelta(minutes=5)
ACCESS_SNAP_RADIUS_M = 300.0
"""A position this close to a place uses the place's transport access point."""
DEPARTURE_LOOKAHEAD = timedelta(hours=2)

# --- synchronisation ----------------------------------------------------------------------------

ICS_PAST = timedelta(days=1)
ICS_FUTURE = timedelta(days=60)
ICS_MARGIN = timedelta(days=7)
"""Parsed beyond the reconciled window so events moving out of it are seen as changed."""
MAX_ICS_BYTES = 10 * 1024 * 1024
ICS_SUSPECT_MIN_REMOVALS = 3
ICS_SUSPECT_REMOVAL_RATIO = 0.5
NEAR_TERM_CHANGE = timedelta(days=7)
MAIL_LOOKBACK = timedelta(days=14)
MAX_REDIRECTS = 5
REDIRECTS = frozenset({301, 302, 303, 307, 308})

# --- worker, notifications, retention -----------------------------------------------------------

TICK_SECONDS = 30
NIGHTLY_REPLAN = time(3, 0)
STALE_PLAN_AFTER = timedelta(hours=6)
REPLAN_RETRY_AFTER = timedelta(minutes=15)
"""Minimum delay between automatic attempts; also the refresh pace while journeys are not
confirmed by MOTIS (outage)."""
REMINDER_GRACE = timedelta(hours=6)
"""A reminder missed during an outage still fires after a restart, up to this late."""
RETENTION_HOUR = 4
NOTIFICATION_RETENTION = timedelta(days=90)
PROPOSAL_RETENTION = timedelta(days=180)
PLAN_PROPOSAL_VALIDITY = timedelta(minutes=15)
WORKER_HEARTBEAT_MAX_AGE = timedelta(minutes=5)
VERSION_RETENTION = timedelta(days=30)
ACTIVITY_RETENTION = timedelta(days=180)
CONVERSATION_RETENTION = timedelta(days=7)
IDEMPOTENCY_RETENTION = timedelta(days=2)

# --- assistant and API --------------------------------------------------------------------------

HISTORY_TURNS = 12
HISTORY_WINDOW = timedelta(hours=24)
"""Recent turns of EVERY Assist conversation (one user): clarifications survive."""
MAX_AGENT_STEPS = 16
CONVERSATION_BUDGET_SECONDS = 60.0
"""No new model call after this; Home Assistant waits longer (one re-plan + the answer)."""
MIN_CALL_SECONDS = 5.0
MAX_TOOL_RESULT_CHARS = 30_000
AUTH_MAX_FAILURES = 10
AUTH_FAILURE_WINDOW = timedelta(minutes=1)
AUTH_LOCKOUT = timedelta(minutes=5)
CALENDAR_PAST = timedelta(days=14)
CALENDAR_FUTURE = timedelta(days=60)
MAX_SCHEDULE_SPAN = timedelta(days=62)
DOCKER_SECRETS_DIR = Path("/run/secrets")

BUFFER = timedelta(minutes=BUFFER_MINUTES)
MAX_JOURNEY = timedelta(minutes=MAX_JOURNEY_MINUTES)
