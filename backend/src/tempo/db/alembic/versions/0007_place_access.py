"""transport access points of places

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-25 23:30:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("place", sa.Column("access_place_id", sa.String(length=50), nullable=True))
    op.add_column("place", sa.Column("access_minutes", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("fk_place_access_place_id_place"),
        "place",
        "place",
        ["access_place_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_place_access_complete"),
        "place",
        "(access_place_id IS NULL) = (access_minutes IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_place_access_minutes_range"),
        "place",
        "access_minutes IS NULL OR access_minutes BETWEEN 1 AND 240",
    )
    op.create_check_constraint(
        op.f("ck_place_access_other"), "place", "access_place_id IS NULL OR access_place_id <> id"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_place_access_other"), "place", type_="check")
    op.drop_constraint(op.f("ck_place_access_minutes_range"), "place", type_="check")
    op.drop_constraint(op.f("ck_place_access_complete"), "place", type_="check")
    op.drop_constraint(op.f("fk_place_access_place_id_place"), "place", type_="foreignkey")
    op.drop_column("place", "access_minutes")
    op.drop_column("place", "access_place_id")
