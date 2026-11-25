"""Prepare the nine Docker secrets of the Swarm stack in deploy/secrets/swarm/ (git-ignored).

One file per secret, named like the secret: create each in Portainer (Secrets > Add secret,
paste the file content) or with ``docker secret create <name> <file>``. Generated values
(database passwords and URLs, API token) are written once and never rotated by a re-run; the
user's credentials already validated locally are copied from deploy/secrets/. Prints no value.

    python deploy/make_swarm_secrets.py
"""

import secrets
import shutil
from pathlib import Path

SOURCE = Path(__file__).parent / "secrets"
TARGET = SOURCE / "swarm"
COPIED = (
    "tempo_ics_url",
    "tempo_bmc_partner_key",
    "tempo_gcp_credentials",
    "tempo_training_imap_password",
)


def write_once(name: str, value: str) -> str:
    path = TARGET / name
    if path.exists():
        print(f"kept      {name}")
        return path.read_text(encoding="utf-8")
    path.write_text(value, encoding="utf-8")
    print(f"generated {name}")
    return value


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    owner = write_once("tempo_owner_db_password", secrets.token_hex(24))
    app = write_once("tempo_app_db_password", secrets.token_hex(24))
    write_once(
        "tempo_owner_database_url",
        f"postgresql+psycopg://tempo_owner:{owner}@postgres:5432/tempo",
    )
    write_once("tempo_database_url", f"postgresql+psycopg://tempo_app:{app}@postgres:5432/tempo")
    write_once("tempo_api_token", secrets.token_urlsafe(32))
    missing = []
    for name in COPIED:
        source = SOURCE / name
        if not source.is_file():
            missing.append(name)
            continue
        shutil.copyfile(source, TARGET / name)
        print(f"copied    {name}")
    if missing:
        print("MISSING (put them in deploy/secrets/ first):", ", ".join(missing))
        return 1
    print(f"9 secrets ready in {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
