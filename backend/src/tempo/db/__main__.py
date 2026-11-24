"""One-shot database jobs: ``python -m tempo.db upgrade`` and
``python -m tempo.db grant-runtime <role>``.

Reads only the database URL (env ``TEMPO_DATABASE_URL`` or Docker secret
``/run/secrets/tempo_database_url``), never the API token: the migration role is separate from
the runtime role (README.md). Concurrent runs are serialised by an advisory lock.
"""

import re
import sys
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text

from tempo.db.migrations import upgrade_to_head

SECRETS_DIR = Path("/run/secrets")


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TEMPO_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=SECRETS_DIR if SECRETS_DIR.is_dir() else None,
    )

    database_url: SecretStr


ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
USAGE = "usage: python -m tempo.db upgrade | grant-runtime <role>"


def grant_runtime(database_url: str, role: str) -> None:
    """Least privilege for the runtime role (run as the migration/owner role after upgrade):
    DML on application tables, sequences usage, ``audit_entry`` append-only (no UPDATE/DELETE),
    no DDL and no access to ``alembic_version`` writes."""
    if not ROLE.match(role):
        raise ValueError("invalid role name")
    statements = [
        f"GRANT USAGE ON SCHEMA public TO {role}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
        f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_entry FROM {role}",
        f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON alembic_version FROM {role}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
        f"REVOKE CREATE ON SCHEMA public FROM {role}",
    ]
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
    finally:
        engine.dispose()


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("upgrade", "grant-runtime"):
        print(USAGE, file=sys.stderr)
        return 2
    settings = MigrationSettings()
    url = settings.database_url.get_secret_value()
    if argv == ["upgrade"]:
        upgrade_to_head(url)
        print("database schema at head")
        return 0
    if len(argv) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    grant_runtime(url, argv[1])
    print(f"runtime privileges granted to {argv[1]}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
