"""ImapSource against a REAL IMAP server (local GreenMail container, test data only).

Requires ``TEMPO_TEST_IMAP=127.0.0.1`` with GreenMail listening on 3025 (SMTP) / 3993 (IMAPS),
see README.md. SKIPPED otherwise.
"""

import os
import smtplib
import ssl
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from tempo.sync.training_mail import ImapSource

HOST = os.environ.get("TEMPO_TEST_IMAP")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not HOST, reason="TEMPO_TEST_IMAP not set: real IMAP not tested"),
]


def send(
    user: str,
    sender: str,
    subject: str,
    body: str,
    display: str | None = None,
    attachment: bytes | None = None,
) -> None:
    message = EmailMessage()
    message["From"] = f"{display} <{sender}>" if display else sender
    message["To"] = user
    message["Subject"] = subject
    message["Message-ID"] = f"<{uuid.uuid4()}@test>"
    message.set_content(body)
    if attachment is not None:
        message.add_attachment(
            attachment,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="Plan Matis (1).docx",
        )
    assert HOST is not None
    with smtplib.SMTP(HOST, 3025, timeout=10) as smtp:
        smtp.send_message(message)


def test_only_the_authorised_sender_is_read() -> None:
    assert HOST is not None
    user = f"student-{uuid.uuid4().hex[:8]}@localhost"
    send(user, "coach@club.test", "Programme", "mardi 15/12 : footing 40 min", "Coach")
    send(user, "bank@bank.test", "Relevé", "secret financial content")
    send(user, "someone@club.test", "Spoof", "coach@club.test lundi 14/12 : 10 km")
    # Test server certificate is self-signed: verification disabled ONLY in this test.
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    since = datetime.now(UTC).date() - timedelta(days=1)
    strict = ImapSource(HOST, user, user, "coach@club.test", port=3993, ssl_context=insecure)
    assert strict.fetch(since) == []  # no Authentication-Results from this test server
    source = ImapSource(
        HOST,
        user,
        user,
        "coach@club.test",
        port=3993,
        ssl_context=insecure,
        require_authentication=False,
    )
    messages = source.fetch(since)
    assert [m.subject for m in messages] == ["Programme"]
    assert messages[0].sender == "coach@club.test"
    assert "footing 40 min" in messages[0].body


def test_invalid_sender_configuration_is_refused() -> None:
    with pytest.raises(ValueError):
        ImapSource("h", "u", "p", 'coach@x.test" OR ALL')


def test_coach_docx_attachment_is_extracted() -> None:
    from tests.docx_fixtures import plan_docx

    assert HOST is not None
    user = f"student-{uuid.uuid4().hex[:8]}@localhost"
    docx = plan_docx()
    send(user, "coach@club.test", "plan", "Confirme la réception", "Jean-Charles", docx)
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    source = ImapSource(
        HOST,
        user,
        user,
        "coach@club.test",
        port=3993,
        ssl_context=insecure,
        require_authentication=False,
    )
    [message] = source.fetch(datetime.now(UTC).date() - timedelta(days=1))
    assert message.subject == "plan" and "Confirme" in message.body
    assert message.attachments == (("Plan Matis (1).docx", docx),)
