"""persistent questions and the preferred time of day of trainings

- ``notification``: ``due_at`` (urgency of a question) and ``resolved_at`` (answered or no
  longer raised; Home Assistant acknowledging a notification on delivery does not resolve it).
- ``training_preferences.mornings`` (bool) becomes ``time_of_day`` (``morning`` or none), in
  the permanent setting and in temporary preferences.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-27 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TO_TIME_OF_DAY = """
CASE WHEN prefs ? 'mornings'
     THEN (prefs - 'mornings') || jsonb_build_object(
          'time_of_day', CASE WHEN (prefs->>'mornings')::boolean THEN 'morning' ELSE 'none' END)
     ELSE prefs END
"""
TO_MORNINGS = """
CASE WHEN prefs ? 'time_of_day'
     THEN (prefs - 'time_of_day') || jsonb_build_object(
          'mornings', prefs->>'time_of_day' = 'morning')
     ELSE prefs END
"""


def _convert(expression: str) -> None:
    op.execute(
        "UPDATE app_setting SET value = jsonb_set(value, '{value}', "  # noqa: S608 - constant SQL
        f"(SELECT {expression} FROM (SELECT value->'value' AS prefs) AS p)) "
        "WHERE key = 'training_preferences' AND jsonb_typeof(value->'value') = 'object'"
    )
    op.execute(
        "UPDATE app_setting SET value = jsonb_set(value, '{value}', COALESCE(("  # noqa: S608 - constant SQL
        "SELECT jsonb_agg(CASE WHEN item->>'key' = 'training_preferences' "
        f"THEN jsonb_set(item, '{{value}}', (SELECT {expression} FROM "
        "(SELECT item->'value' AS prefs) AS p)) ELSE item END) "
        "FROM jsonb_array_elements(value->'value') AS item), '[]'::jsonb)) "
        "WHERE key = 'temporary_preferences' AND jsonb_typeof(value->'value') = 'array'"
    )


def upgrade() -> None:
    op.add_column("notification", sa.Column("due_at", sa.DateTime(timezone=True)))
    op.add_column("notification", sa.Column("resolved_at", sa.DateTime(timezone=True)))
    # Existing questions start unresolved (reading one in Home Assistant never answered it):
    # the next re-plan closes those it no longer raises.
    _convert(TO_TIME_OF_DAY)


def downgrade() -> None:
    _convert(TO_MORNINGS)
    op.drop_column("notification", "resolved_at")
    op.drop_column("notification", "due_at")
