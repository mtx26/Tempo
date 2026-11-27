"""Keep the last successful job run through later failures."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_run", sa.Column("last_success_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE job_run SET last_success_at = last_finished_at WHERE last_status = 'ok'")


def downgrade() -> None:
    op.drop_column("job_run", "last_success_at")
