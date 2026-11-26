"""Programmatic Alembic entry points (used by the service at start-up and by tests)."""

from pathlib import Path

from alembic import command
from alembic.config import Config

ALEMBIC_DIR = Path(__file__).parent / "alembic"


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_to_head(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")


def downgrade_to(database_url: str, revision: str) -> None:
    """Roll the schema back to ``revision`` (data kept where the older schema can hold it:
    see each migration's ``downgrade``)."""
    command.downgrade(alembic_config(database_url), revision)
