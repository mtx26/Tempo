"""Least-privilege runtime role on a REAL PostgreSQL (two-role model, append-only audit)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ProgrammingError

from tempo.db.__main__ import grant_runtime

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime_url(database_url: str, engine: Engine) -> Iterator[str]:
    role = f"tempo_app_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
    grant_runtime(database_url, role)
    url = make_url(database_url).set(username=role, password=password)
    yield url.render_as_string(hide_password=False)
    with engine.begin() as conn:
        conn.execute(text(f"DROP OWNED BY {role}"))
        conn.execute(text(f"DROP ROLE {role}"))


def test_runtime_role_has_dml_but_audit_is_append_only(runtime_url: str) -> None:
    app = create_engine(runtime_url)
    now = datetime.now(UTC)
    try:
        with app.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO notification (id, kind, title, message, dedupe_key, created_at) "
                    "VALUES (:id, 'k', 't', 'm', :key, :now)"
                ),
                {"id": uuid.uuid4(), "key": uuid.uuid4().hex, "now": now},
            )
            audit_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO audit_entry (id, at, actor, action, entity, entity_id, details) "
                    "VALUES (:id, :now, 'test', 'a', 'e', '1', '{}')"
                ),
                {"id": audit_id, "now": now},
            )
            assert conn.execute(text("SELECT count(*) FROM audit_entry")).scalar_one() >= 1
        forbidden = [
            ("UPDATE audit_entry SET actor = 'x' WHERE id = :id", {"id": audit_id}),
            ("DELETE FROM audit_entry WHERE id = :id", {"id": audit_id}),
            ("UPDATE alembic_version SET version_num = 'x'", {}),
            ("CREATE TABLE evil (id int)", {}),
            ("DROP TABLE notification", {}),
        ]
        for statement, params in forbidden:
            with (
                pytest.raises(ProgrammingError, match=r"permission denied|must be owner"),
                app.begin() as conn,
            ):
                conn.execute(text(statement), params)
    finally:
        app.dispose()


def test_invalid_role_name_is_refused(database_url: str) -> None:
    with pytest.raises(ValueError):
        grant_runtime(database_url, "x; DROP TABLE audit_entry")
