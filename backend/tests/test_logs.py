"""Logging setup shared by every entry point: no secret-bearing URL in the logs."""

import logging

from tempo.logs import configure_logging


def test_http_client_request_lines_are_silenced() -> None:
    configure_logging()
    for name in ("httpx2", "httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("tempo").getEffectiveLevel() <= logging.INFO
