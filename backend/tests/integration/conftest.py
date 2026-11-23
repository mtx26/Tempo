"""PostgreSQL integration fixtures.

Requires ``TEMPO_TEST_DATABASE_URL`` (see scripts/dev_postgres.py). Without it, every test in
this package is SKIPPED with an explicit reason — never silently passed.
"""

import os
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from tempo.db.migrations import alembic_config, upgrade_to_head

BASE_URL = os.environ.get("TEMPO_TEST_DATABASE_URL")


def downgrade_to_base(database_url: str) -> None:
    """Drop the whole schema: test-only helper, deliberately absent from production code."""
    command.downgrade(alembic_config(database_url), "base")


pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    if not BASE_URL:
        pytest.skip("TEMPO_TEST_DATABASE_URL not set: PostgreSQL integration tests not run")
    admin = create_engine(BASE_URL, isolation_level="AUTOCOMMIT")
    name = f"tempo_test_{uuid.uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(BASE_URL).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    upgrade_to_head(database_url)
    eng = create_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Each test runs in a transaction that is rolled back afterwards (isolation)."""
    with engine.connect() as conn:
        trans = conn.begin()
        with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
            yield s
        trans.rollback()
