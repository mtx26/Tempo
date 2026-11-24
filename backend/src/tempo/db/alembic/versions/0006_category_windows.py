"""category windows (B07)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24 19:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "category_window",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_minute", sa.Integer(), nullable=False),
        sa.Column("end_minute", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('training', 'personal', 'task')", name=op.f("ck_category_window_kind")
        ),
        sa.CheckConstraint(
            "weekday BETWEEN 0 AND 6", name=op.f("ck_category_window_weekday_range")
        ),
        sa.CheckConstraint(
            "end_minute > start_minute", name=op.f("ck_category_window_positive_duration")
        ),
        sa.CheckConstraint(
            "start_minute >= 0 AND end_minute <= 1440",
            name=op.f("ck_category_window_minute_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_category_window")),
    )


def downgrade() -> None:
    op.drop_table("category_window")
