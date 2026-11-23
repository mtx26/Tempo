"""One-shot migration job: ``python -m tempo.db upgrade``.

Reads only the database URL (env ``TEMPO_DATABASE_URL`` or Docker secret
``/run/secrets/tempo_database_url``), never the API token: the migration role is separate from
the runtime role (docs/DEPLOYMENT.md). Concurrent runs are serialised by an advisory lock (D018).
"""

import sys
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

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


def main(argv: list[str]) -> int:
    if argv != ["upgrade"]:
        print("usage: python -m tempo.db upgrade", file=sys.stderr)
        return 2
    settings = MigrationSettings()
    upgrade_to_head(settings.database_url.get_secret_value())
    print("database schema at head")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
