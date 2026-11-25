"""Process entry point of the private Tempo API (``python -m tempo.api``).

Binds the container port on the internal overlay network only; Home Assistant reaches it there.
``server_header=False`` and no proxy headers (nothing sits in front of it). Authentication:
bearer token with rotation and a limiter of failed attempts (``api.auth``).
"""

import logging

import uvicorn

from tempo.api.app import create_app
from tempo.config import Settings

PORT = 8000


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_app(Settings()),  # values come from env / Docker secrets
        host="0.0.0.0",  # noqa: S104 - container port, internal overlay network only
        port=PORT,
        server_header=False,
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
