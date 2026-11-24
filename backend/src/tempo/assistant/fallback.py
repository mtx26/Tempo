"""Deterministic conversation without any LLM (no key, outage, refusal): ARCHITECTURE.md §4.

Understands a fixed set of phrasings (``ai.actions``, ``ai.questions``, ``ai.text_parser``) and
executes every change through the operation catalogue (``run_operation``), exactly like the
LLM assistant: same validation, savepoints, audit and re-plan requests.
"""

from datetime import datetime
from typing import Any

from tempo.ai.actions import Action, block_to_complete, parse_action, pending_proposals, subject_of
from tempo.ai.apply import MAX_ITEMS, activity_values, known_places, task_values
from tempo.ai.questions import answer, parse_question
from tempo.ai.text_parser import parse_text
from tempo.assistant.catalog import OpContext, run_operation

EXAMPLES = (
    "Exemples : « ajoute 5h de maths pour vendredi », « ajoute 90 min d'analyse avant le "
    "18/12 strict », « mercredi 16/12 18h30 : fractionné 60 min @club - 6 x 800 m », "
    "« course samedi 19/12 10:00 : 10 km 60 min @namur », « replanifie », « qu'est-ce que "
    "j'ai demain ? », « prochain départ ? », « combien d'heures d'étude il me reste ? »."
)


def _reply(summary: str, changed: bool) -> dict[str, Any]:
    return {"reply": summary, "changed": changed}


def _act(ctx: OpContext, action: Action) -> dict[str, Any]:
    if action.kind in ("approve", "reject"):
        pending = pending_proposals(ctx.session)
        if not pending:
            return _reply("Aucune proposition en attente.", False)
        if len(pending) > 1:
            titles = " ; ".join(p.title for p in pending[:5])
            return _reply(
                f"{len(pending)} propositions en attente ({titles}). Précisez laquelle : je ne "
                "décide pas à votre place.",
                False,
            )
        outcome = run_operation(
            ctx,
            "decide_proposal",
            {"proposal_id": str(pending[0].id), "approve": action.kind == "approve"},
        )
        if not outcome["ok"]:
            return _reply(f"Refusé : {outcome['error']}.", False)
        decided = outcome["result"]["proposal"]
        verb = {"approved": "approuvée", "rejected": "refusée"}.get(
            decided["status"], "devenue obsolète (ses entrées ont changé), rien appliqué"
        )
        return _reply(f"Proposition {verb} : {decided['title']}.", True)
    block = block_to_complete(ctx.session, ctx.now, ctx.tz)
    if block is None:
        return _reply(
            "Je ne trouve pas de session d'étude en cours ou terminée aujourd'hui.", False
        )
    outcome = run_operation(ctx, "log_work", {"block_id": str(block.id)})
    if not outcome["ok"]:
        return _reply(f"Refusé : {outcome['error']}.", False)
    start = block.start_at.astimezone(ctx.tz).strftime("%H:%M")
    return _reply(
        f"Session « {subject_of(ctx.session, block)} » de {start} marquée comme faite.", True
    )


def _activity_arguments(values: dict[str, Any], tz: Any) -> dict[str, Any]:
    local = {k: v.astimezone(tz).isoformat() for k, v in values.items() if isinstance(v, datetime)}
    common = {
        "kind": values["kind"],
        "title": values["title"],
        "notes": values["notes"],
        "duration_minutes": values["duration_minutes"],
        "place_id": values["place_id"],
    }
    if values["kind"] == "race":
        return {**common, "scheduling": "fixed", "start": local["earliest_start_at"]}
    return {
        **common,
        "scheduling": "flexible",
        "window_start": local["earliest_start_at"],
        "window_end": local["latest_end_at"],
        "preferred_start": local.get("preferred_start_at"),
    }


def reply(ctx: OpContext, text: str) -> dict[str, Any]:
    """Answer ``text`` deterministically: ``{"reply": str, "changed": bool}``."""
    action = parse_action(text)
    if action is not None:
        return _act(ctx, action)
    today = ctx.now.astimezone(ctx.tz).date()
    question = parse_question(text, today)
    if question is not None:
        return _reply(answer(ctx.session, question, ctx.now, ctx.tz), False)
    result = parse_text(text, today)
    if len(result.tasks) + len(result.activities) > MAX_ITEMS:
        return _reply(f"Refusé : plus de {MAX_ITEMS} éléments dans un seul message.", False)
    created: list[str] = []
    rejected: list[str] = []
    for task in result.tasks:
        values, reason = task_values(task, today)
        if values is None:
            rejected.append(reason or "tâche invalide")
            continue
        outcome = run_operation(
            ctx,
            "create_task",
            {
                "title": task.subject,
                "estimated_minutes": task.effort_minutes,
                "target_date": task.target_date.isoformat(),
                "hard_deadline": task.hard,
            },
        )
        if outcome["ok"]:
            kind = "stricte" if task.hard else "souple"
            created.append(
                f"tâche « {task.subject} » {task.effort_minutes} min, échéance {kind} "
                f"{task.target_date.isoformat()}"
            )
        else:
            rejected.append(f"{task.subject} : {outcome['error']}")
    places = known_places(ctx.session)
    for activity in result.activities:
        values, reason = activity_values(activity, ctx.tz, today, places)
        if values is None:
            rejected.append(reason or "activité invalide")
            continue
        outcome = run_operation(ctx, "create_event", _activity_arguments(values, ctx.tz))
        if not outcome["ok"]:
            rejected.append(f"{activity.title} : {outcome['error']}")
            continue
        label = "course" if activity.kind == "race" else "entraînement"
        where = " (lieu inconnu)" if activity.place_id and not values["place_id"] else ""
        at = f" à {activity.start.strftime('%H:%M')}" if activity.start else ""
        created.append(
            f"{label} « {activity.title} » le {activity.day.isoformat()}{at}, "
            f"{activity.duration_minutes} min{where}"
        )
    replanned = False
    if result.replan:
        outcome = run_operation(ctx, "plan_now", {})
        replanned = outcome["ok"]
        if not outcome["ok"]:
            rejected.append(f"replanification : {outcome['error']}")
    parts = []
    if created:
        parts.append("Ajouté : " + " ; ".join(created) + ".")
    if replanned:
        parts.append("Replanification effectuée.")
    if rejected:
        parts.append("Refusé : " + " ; ".join(rejected) + ".")
    if not parts:
        parts.append("Je n'ai pas compris la demande. " + EXAMPLES)
    return _reply(" ".join(parts), bool(created) or replanned)
