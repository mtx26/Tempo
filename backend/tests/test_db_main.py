"""Migration job entry point (``python -m tempo.db upgrade``)."""

import pytest

from tempo.db import __main__ as job


def test_usage_error_without_upgrade_argument() -> None:
    assert job.main([]) == 2
    assert job.main(["downgrade"]) == 2


def test_upgrade_reads_only_the_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(job, "upgrade_to_head", seen.append)
    monkeypatch.delenv("TEMPO_API_TOKEN", raising=False)
    monkeypatch.setenv("TEMPO_DATABASE_URL", "postgresql+psycopg://u:p@db:5432/tempo")
    assert job.main(["upgrade"]) == 0
    assert seen == ["postgresql+psycopg://u:p@db:5432/tempo"]
