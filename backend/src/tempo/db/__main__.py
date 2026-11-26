"""One-shot database jobs: ``python -m tempo.db upgrade``, ``python -m tempo.db downgrade
<revision>`` (rollback to an older release, run with the NEWER image before switching back:
README.md) and ``python -m tempo.db grant-runtime <role>``.

Reads only the database URL (env ``TEMPO_DATABASE_URL`` or Docker secret
``/run/secrets/tempo_database_url``), never the API token: the migration role is separate from
the runtime role (README.md). Concurrent runs are serialised by an advisory lock.
"""

import re
import sys

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text

from tempo.const import DOCKER_SECRETS_DIR
from tempo.db.migrations import downgrade_to, upgrade_to_head


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TEMPO_",
        extra="ignore",
        hide_input_in_errors=True,
        secrets_dir=DOCKER_SECRETS_DIR if DOCKER_SECRETS_DIR.is_dir() else None,
    )

    database_url: SecretStr


ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
USAGE = "usage: python -m tempo.db upgrade | downgrade <revision> | grant-runtime <role>"
REVISION = re.compile(r"^\d{4}$")


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
    valid = argv == ["upgrade"] or (
        len(argv) == 2
        and (argv[0] == "grant-runtime" or (argv[0] == "downgrade" and REVISION.match(argv[1])))
    )
    if not valid:
        print(USAGE, file=sys.stderr)
        return 2
    settings = MigrationSettings()
    url = settings.database_url.get_secret_value()
    if argv == ["upgrade"]:
        upgrade_to_head(url)
        print("database schema at head")
        return 0
    if argv[0] == "downgrade":
        downgrade_to(url, argv[1])
        print(f"database schema at {argv[1]}")
        return 0
    grant_runtime(url, argv[1])
    print(f"runtime privileges granted to {argv[1]}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
