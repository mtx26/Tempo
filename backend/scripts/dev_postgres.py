"""Start a throw-away local PostgreSQL for integration tests (development only).

Run with an interpreter that has ``pgserver`` wheels (Python 3.12):

    uv run --no-project --python 3.12 --with pgserver python scripts/dev_postgres.py .pgdata

It prints ``TEMPO_TEST_DATABASE_URL=...`` and keeps the server alive until interrupted.
The server listens on 127.0.0.1 only and must never hold real personal data.
"""

import sys
import time
from pathlib import Path

import pgserver  # type: ignore[import-not-found]

data_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".pgdata").resolve()
server = pgserver.get_server(str(data_dir), cleanup_mode="stop")
listen = server.psql("SHOW listen_addresses;")
if "*" in listen or "0.0.0.0" in listen:  # noqa: S104 - this is the check, not a bind
    server.cleanup()
    raise SystemExit(f"refusing to run: PostgreSQL listens beyond localhost ({listen.strip()})")
uri = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
print(f"TEMPO_TEST_DATABASE_URL={uri}", flush=True)
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    server.cleanup()
