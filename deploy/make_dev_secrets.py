"""Generate LOCAL development secrets for deploy/compose.dev.yml (never for production).

Writes deploy/secrets/{postgres_password,tempo_database_url,tempo_api_token}; the folder is
git-ignored. Existing files are kept (re-running does not rotate secrets). Prints no secret.
"""

import secrets
from pathlib import Path

FOLDER = Path(__file__).parent / "secrets"


def write_once(name: str, value: str) -> None:
    path = FOLDER / name
    if path.exists():
        print(f"kept {path}")
        return
    path.write_text(value, encoding="utf-8")
    print(f"wrote {path}")


def main() -> None:
    FOLDER.mkdir(exist_ok=True)
    existing = FOLDER / "postgres_password"
    password = existing.read_text(encoding="utf-8") if existing.exists() else secrets.token_hex(24)
    write_once("postgres_password", password)
    write_once(
        "tempo_database_url", f"postgresql+psycopg://tempo:{password}@postgres:5432/tempo"
    )
    write_once("tempo_api_token", secrets.token_urlsafe(32))


if __name__ == "__main__":
    main()
