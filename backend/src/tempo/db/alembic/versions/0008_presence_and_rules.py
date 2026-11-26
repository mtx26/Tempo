"""one presence model and consolidated rules

- Study availability is time only (the user studies wherever they are): ``place_id`` removed
  from weekly windows and dated exceptions.
- Dated exceptions apply to every flexible activity (``kind`` NULL) or to one category.
- Activities: ``indoor`` (darkness only matters outdoors) and ``rest_days`` (explicit
  recovery before a training, never inferred).
- Settings: the residence change is no longer an arbitrary 12:00 (it happens after the last
  class of the change day); removed study preferences are dropped (daily cap, per-subject daily
  count, the old 30-minute same-subject gap replaced by alternation and breaks).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-26 15:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_availability_window_place_id_place", "availability_window", type_="foreignkey"
    )
    op.drop_column("availability_window", "place_id")
    op.drop_constraint(
        "fk_availability_exception_place_id_place", "availability_exception", type_="foreignkey"
    )
    op.drop_column("availability_exception", "place_id")
    op.add_column("availability_exception", sa.Column("kind", sa.String(length=10)))
    op.create_check_constraint(
        op.f("ck_availability_exception_kind"),
        "availability_exception",
        "kind IS NULL OR kind IN ('study', 'training', 'personal', 'task')",
    )
    op.add_column(
        "activity", sa.Column("indoor", sa.Boolean(), server_default="false", nullable=False)
    )
    op.add_column(
        "activity", sa.Column("rest_days", sa.Integer(), server_default="0", nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_activity_rest_days_range"), "activity", "rest_days BETWEEN 0 AND 7"
    )
    # The old default change time (12:00) was never stated by the user.
    op.execute(
        "UPDATE app_setting SET value = jsonb_set(value, '{value,switch_at}', 'null') "
        "WHERE key = 'residence_alternation' AND value->'value'->>'switch_at' = '12:00'"
    )
    op.execute(
        "UPDATE app_setting SET value = jsonb_set(value, '{value}', (value->'value') "
        "- 'max_daily_study_minutes' - 'max_blocks_per_subject_per_day' "
        "- 'min_gap_same_subject_minutes') "
        "WHERE key = 'study_preferences' AND jsonb_typeof(value->'value') = 'object'"
    )


def downgrade() -> None:
    """Back to the 0.6 schema WITHOUT losing the study windows: the place they need again is
    the first residence of the alternation (the only place 0.6 could plan from); rows are
    removed only when no residence exists. What 0.6 cannot represent is dropped and
    documented: per-category unavailability other than study (0.6 applied exceptions to study
    only), ``indoor`` and ``rest_days`` of activities. A stated change time is kept; without
    one, 0.6 needs a fixed time again (12:00, its old default)."""
    op.drop_constraint(op.f("ck_activity_rest_days_range"), "activity", type_="check")
    op.drop_column("activity", "rest_days")
    op.drop_column("activity", "indoor")
    op.execute("DELETE FROM availability_exception WHERE kind IS NOT NULL AND kind <> 'study'")
    op.drop_constraint(
        op.f("ck_availability_exception_kind"), "availability_exception", type_="check"
    )
    op.drop_column("availability_exception", "kind")
    residence = (
        "(SELECT value->'value'->'residences'->>0 FROM app_setting "
        "WHERE key = 'residence_alternation')"
    )
    for table in ("availability_window", "availability_exception"):
        op.add_column(
            table,
            sa.Column(
                "place_id",
                sa.String(length=50),
                sa.ForeignKey("place.id", ondelete="CASCADE", name=f"fk_{table}_place_id_place"),
            ),
        )
    op.execute(f"UPDATE availability_window SET place_id = {residence}")  # noqa: S608 - constant SQL
    op.execute("DELETE FROM availability_window WHERE place_id IS NULL")
    op.alter_column("availability_window", "place_id", nullable=False)
    op.execute(f"UPDATE availability_exception SET place_id = {residence} WHERE available")  # noqa: S608 - constant SQL
    op.execute(
        "UPDATE app_setting SET value = jsonb_set(value, '{value,switch_at}', '\"12:00\"') "
        "WHERE key = 'residence_alternation' AND (value->'value'->>'switch_at') IS NULL"
    )
