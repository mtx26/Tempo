"""Logging setup shared by every Tempo entry point (API, worker, relay, routing-data update).

HTTP client libraries log every request line with its full URL at INFO level; some URLs are
secrets (the private iCal feed URL), so those loggers only report warnings and errors.
"""

import logging

QUIET = ("httpx2", "httpx", "httpcore")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger().setLevel(logging.INFO)
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
