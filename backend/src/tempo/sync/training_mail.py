"""Training e-mails from ONE explicitly authorised sender (milestone 5).

Access is narrow by construction (docs/REQUIREMENTS.md: never the whole mailbox):
- the mailbox is opened read-only (``EXAMINE``) and searched server-side with
  ``FROM <authorised sender> SINCE <date>``; other messages are never downloaded;
- each downloaded message's ``From`` address is checked again (exact match) and discarded
  otherwise; message bodies are never logged;
- e-mail ``From`` can be forged, so only flexible trainings parsed deterministically are added
  directly. Races (fixed commitments) and anything interpreted by the optional LLM become
  ``training_import`` proposals that the user approves in Home Assistant.
"""

import email
import imaplib
import logging
import re
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parseaddr
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from tempo.ai.apply import apply_result, from_extraction, known_places, result_to_json
from tempo.ai.llm import Interpreter
from tempo.ai.text_parser import ParseResult, parse_text
from tempo.planning.service import notify, propose, set_setting

logger = logging.getLogger(__name__)
MAX_MESSAGES = 50
MAX_BODY_CHARS = 20_000
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True, slots=True)
class MailMessage:
    message_id: str
    sender: str
    subject: str
    body: str


class MailSource(Protocol):
    def fetch(self, since: date) -> list[MailMessage]: ...


def _imap_date(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _text_body(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    text = content if isinstance(content, str) else ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return text[:MAX_BODY_CHARS]


class ImapSource:
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        allowed_sender: str,
        *,
        mailbox: str = "INBOX",
        port: int = 993,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if "@" not in allowed_sender or any(c in allowed_sender for c in '"\\\r\n '):
            raise ValueError("allowed sender must be one plain e-mail address")
        self._host, self._port, self._user, self._password = host, port, user, password
        self._sender = allowed_sender.casefold()
        self._mailbox = mailbox
        # Certificate verification is ON by default; only tests inject another context.
        self._ssl = ssl_context or ssl.create_default_context()

    def fetch(self, since: date) -> list[MailMessage]:
        messages: list[MailMessage] = []
        with imaplib.IMAP4_SSL(self._host, self._port, ssl_context=self._ssl, timeout=60) as imap:
            imap.login(self._user, self._password)
            status, _ = imap.select(self._mailbox, readonly=True)
            if status != "OK":
                raise RuntimeError("cannot open the configured mailbox")
            status, data = imap.uid(
                "SEARCH", "FROM", f'"{self._sender}"', "SINCE", _imap_date(since)
            )
            if status != "OK":
                raise RuntimeError("IMAP search failed")
            uids = (data[0] or b"").split()[-MAX_MESSAGES:]
            for raw_uid in uids:
                uid = raw_uid.decode()
                status, parts = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if status != "OK" or not isinstance(raw, bytes):
                    continue
                message = email.message_from_bytes(raw, policy=default_policy)
                if not isinstance(message, EmailMessage):
                    continue
                sender = parseaddr(str(message.get("From", "")))[1].casefold()
                if sender != self._sender:
                    continue  # server search is not trusted alone
                messages.append(
                    MailMessage(
                        str(message.get("Message-ID", f"uid-{uid}")).strip()[:250],
                        sender,
                        str(message.get("Subject", ""))[:300],
                        _text_body(message),
                    )
                )
        return messages


def _meaningful(lines: Iterable[str]) -> list[str]:
    """Unparsed lines worth reporting: they contain a number (dates, durations)."""
    return [line for line in lines if re.search(r"\d", line)]


def import_messages(
    session: Session,
    messages: list[MailMessage],
    *,
    tz: ZoneInfo,
    today: date,
    interpreter: Interpreter | None,
) -> dict[str, int]:
    stats = {"trainings": 0, "proposals": 0, "unparsed": 0}
    for message in messages:
        parsed = parse_text(message.body, today)
        direct = ParseResult(activities=[a for a in parsed.activities if a.kind == "training"])
        races = ParseResult(activities=[a for a in parsed.activities if a.kind == "race"])
        applied = apply_result(
            session, direct, tz=tz, today=today, source="email", source_ref=message.message_id
        )
        stats["trainings"] += len(applied.created)
        if applied.created:
            notify(
                session,
                "training_added",
                "Entraînements reçus",
                "Ajouté depuis l'e-mail « "
                + message.subject
                + " » : "
                + " ; ".join(applied.created),
                f"training_added:{message.message_id}",
            )
        if races.activities:
            _propose(session, message, races, tz, today, "races")
            stats["proposals"] += 1
        leftover = _meaningful(parsed.unparsed)
        if not leftover:
            continue
        extraction = (
            None
            if interpreter is None
            else interpreter.extract("\n".join(leftover), today, sorted(known_places(session)))
        )
        if extraction is not None:
            converted, _ = from_extraction(extraction)
            converted.tasks = []  # coach e-mails never create study tasks
            if converted.activities:
                _propose(session, message, converted, tz, today, "ai")
                stats["proposals"] += 1
                continue
        stats["unparsed"] += len(leftover)
        notify(
            session,
            "training_unparsed",
            "E-mail d'entraînement non compris",
            f"{len(leftover)} ligne(s) de « {message.subject} » n'ont pas été reconnues. "
            "Format attendu : « mercredi 16/12 18h30 : fractionné 60 min @club - contenu ».",
            f"training_unparsed:{message.message_id}",
        )
    if stats["trainings"]:
        set_setting(session, "replan_requested", datetime.now(UTC).isoformat())
    return stats


def _propose(
    session: Session,
    message: MailMessage,
    result: ParseResult,
    tz: ZoneInfo,
    today: date,
    origin: str,
) -> None:
    titles = ", ".join(a.title for a in result.activities)
    propose(
        session,
        "training_import",
        f"Ajouter depuis l'e-mail : {titles}"[:300],
        {
            "result": result_to_json(result),
            "source_ref": f"{message.message_id}:{origin}",
            "timezone": tz.key,
            "today": today.isoformat(),
            "subject": message.subject,
            "origin": origin,
        },
        f"training_import:{message.message_id}:{origin}",
    )
