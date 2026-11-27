"""Clarification of a scoped, uncommitted proposal."""

import uuid
from datetime import UTC
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import select

from tempo.assistant.catalog import (
    CATALOG,
    OpContext,
    OpError,
    PreviewChange,
    Strict,
    kept_activities,
    resolve_references,
)
from tempo.const import (
    CONVERSATION_DRAFT_KIND,
    CONVERSATION_DRAFT_VALIDITY,
    MAX_PLAN_CHANGES,
)
from tempo.db.models import ProposalRow
from tempo.db.store import propose

ClarificationStatus = Literal["clarification", "missing_information"]


class PreparedChanges(Strict):
    changes: list[PreviewChange] = Field(default_factory=list, max_length=MAX_PLAN_CHANGES)
    keep_activities: list[str] = Field(default_factory=list, max_length=20)


class ClarifyIn(PreparedChanges):
    question: str = Field(min_length=1, max_length=500)
    reason: ClarificationStatus = "clarification"


class ChangeEdit(Strict):
    step: int = Field(ge=1, description="One-based position of an existing prepared step.")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Only fields to replace.")
    remove: bool = False


class ResumeIn(PreparedChanges):
    draft_id: uuid.UUID
    edits: list[ChangeEdit] = Field(default_factory=list, max_length=MAX_PLAN_CHANGES)
    release_activities: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Previously kept activity ids the user explicitly no longer needs to keep.",
    )
    question: str | None = Field(default=None, min_length=1, max_length=500)
    reason: ClarificationStatus = "clarification"


CONTROL_TOOLS: dict[str, tuple[type[Strict], str]] = {
    "clarify_request": (
        ClarifyIn,
        "Pause a new request for a clarification or because a required fact is missing. "
        "Include all known constraints and any complete operations not already called this turn. "
        "No changes will be applied.",
    ),
    "resume_request": (
        ResumeIn,
        "Continue one specific pending draft using the user's answer. Supply only additional "
        "operations, explicit edits to existing steps, and additional activities to keep. "
        "Set question only if another answer is needed. Otherwise request a single preview.",
    ),
}


def active_drafts(ctx: OpContext) -> list[ProposalRow]:
    if ctx.user_id is None:
        return []
    return list(
        ctx.session.scalars(
            select(ProposalRow)
            .where(
                ProposalRow.kind == CONVERSATION_DRAFT_KIND,
                ProposalRow.status == "pending",
                ProposalRow.details["expires_at"].astext > ctx.now.astimezone(UTC).isoformat(),
                ProposalRow.details["user_id"].astext == ctx.user_id,
                ProposalRow.details["conversation_id"].astext == ctx.conversation_id,
            )
            .order_by(ProposalRow.created_at.desc())
        )
    )


def draft_inputs(
    ctx: OpContext,
    body: PreparedChanges,
    candidates: list[PreviewChange],
    draft: ProposalRow | None,
) -> PreparedChanges:
    previous = (
        PreparedChanges()
        if draft is None
        else PreparedChanges.model_validate(
            {
                "changes": draft.details["changes"],
                "keep_activities": draft.details["keep_activities"],
            }
        )
    )
    edits: dict[int, ChangeEdit] = {}
    released: set[str] = set()
    if isinstance(body, ResumeIn):
        for change_edit in body.edits:
            if change_edit.step > len(previous.changes) or change_edit.step in edits:
                raise OpError(f"étape de brouillon introuvable ou répétée : {change_edit.step}")
            edits[change_edit.step] = change_edit
        released.update(body.release_activities)
        if released.difference(previous.keep_activities):
            raise OpError("activité à retirer des contraintes introuvable")
    changes = candidates.copy()
    for index, change in enumerate(previous.changes, start=1):
        edit = edits.get(index)
        if edit is None:
            changes.append(change)
        elif not edit.remove:
            changes.append(
                change.model_copy(
                    update={
                        "arguments": {**change.arguments, **edit.arguments},
                    }
                )
            )
    for extra in body.changes:
        if not any(
            change.name == extra.name
            and change.arguments == extra.arguments
            and (extra.ref is None or extra.ref == change.ref)
            for change in changes
        ):
            changes.append(extra)
    refs: dict[str, Any] = {}
    for change in changes:
        # Check the catalogue schema now. Dependent values get their final validation at replay.
        arguments = resolve_references(change.arguments, refs)
        CATALOG[change.name].model.model_validate(arguments)
        if change.ref is not None:
            if change.ref in refs:
                raise OpError(f"référence d'opération répétée : {change.ref}")
            refs[change.ref] = {
                entity: {"id": str(uuid.UUID(int=0))}
                for entity in ("activity", "task", "reminder", "first")
            }
    retained = [activity for activity in previous.keep_activities if activity not in released]
    ids = list(dict.fromkeys([*retained, *body.keep_activities]))
    kept = kept_activities(ctx, ids)
    if draft is not None and kept[: len(retained)] != [
        activity for activity in draft.details["kept_activities"] if activity["id"] not in released
    ]:
        raise OpError("une activité à conserver a changé depuis la clarification")
    return PreparedChanges(changes=changes, keep_activities=ids)


def save_draft(
    ctx: OpContext,
    draft: ProposalRow | None,
    body: PreparedChanges,
    question: str,
    message: str,
    reason: str,
) -> uuid.UUID:
    if ctx.user_id is None:
        raise OpError("un brouillon nécessite un utilisateur et un fil de conversation")
    details = {
        "changes": [
            change.model_dump(mode="json", exclude_defaults=True) for change in body.changes
        ],
        "keep_activities": body.keep_activities,
        "kept_activities": kept_activities(ctx, body.keep_activities),
        "user_id": ctx.user_id,
        "conversation_id": ctx.conversation_id,
        "question": question,
        "reason": reason,
        "messages": [*(draft.details["messages"] if draft is not None else []), message],
        "expires_at": (ctx.now + CONVERSATION_DRAFT_VALIDITY).astimezone(UTC).isoformat(),
    }
    if draft is not None:
        draft.details = details
        return draft.id
    draft_id = propose(
        ctx.session,
        CONVERSATION_DRAFT_KIND,
        "Demande en attente de précision",
        details,
        f"{CONVERSATION_DRAFT_KIND}:{uuid.uuid4()}",
    )
    if draft_id is None:
        raise OpError("le brouillon n'a pas pu être enregistré")
    return draft_id
