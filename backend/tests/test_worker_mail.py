"""Coach e-mail import is only enabled when the sender can really be authenticated."""

from pydantic import SecretStr

from tempo.sync.training_mail import ImapSource
from tempo.worker.__main__ import WorkerSettings, mail_source


def settings(**extra: object) -> WorkerSettings:
    values: dict[str, object] = {
        "database_url": SecretStr("postgresql+psycopg://x@localhost/x"),
        "training_imap_host": "imap.example",
        "training_imap_user": "me@example",
        "training_imap_password": SecretStr("app-password"),
        "training_allowed_sender": "coach@club.be",
        **extra,
    }
    return WorkerSettings(**values)  # type: ignore[arg-type]


def test_import_is_disabled_without_the_trusted_receiving_server() -> None:
    assert mail_source(settings()) is None


def test_import_is_enabled_with_the_trusted_receiving_server() -> None:
    assert isinstance(mail_source(settings(training_authserv_id="mx.google.com")), ImapSource)
