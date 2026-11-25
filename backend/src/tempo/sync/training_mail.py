"""Training e-mails from ONE explicitly authorised sender.

Access is narrow by construction (ARCHITECTURE.md: never the whole mailbox):
- the mailbox is opened read-only (``EXAMINE``) and searched server-side with
  ``FROM <authorised sender> SINCE <date>``; other messages are never downloaded;
- each downloaded message's ``From`` address is checked again (exact match) and discarded
  otherwise; message bodies are never logged;
- e-mail ``From`` can be forged, so only flexible trainings parsed deterministically are added
  directly. Races (fixed commitments) and anything interpreted by the optional LLM become
  ``training_import`` proposals that the user approves in Home Assistant.
"""

import email
import hashlib
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
from html.parser import HTMLParser
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from tempo.ai.apply import apply_result, from_extraction, known_places, result_to_json
from tempo.ai.llm import Interpreter
from tempo.ai.text_parser import ParseResult, parse_text
from tempo.db.store import get_setting, notify, propose, set_setting
from tempo.sync.coach_import import apply_weekly_plan
from tempo.sync.coach_plan import MAX_DOCX_BYTES, PlanFormatError, parse_plan_docx

logger = logging.getLogger(__name__)
MAX_MESSAGES = 200
MAX_MESSAGE_BYTES = 1_000_000
MAX_BODY_CHARS = 20_000
MAX_SEEN = 2000
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True, slots=True)
class MailMessage:
    message_id: str
    sender: str
    subject: str
    body: str
    attachments: tuple[tuple[str, bytes], ...] = ()
    """``.docx`` attachments only (file name, content), size-checked."""

    @property
    def digest(self) -> str:
        """Idempotency key: Message-ID is chosen by the sender, so the content is included."""
        digest = hashlib.sha256(f"{self.message_id}\n{self.subject}\n{self.body}".encode())
        for name, content in self.attachments:
            digest.update(name.encode())
            digest.update(hashlib.sha256(content).digest())
        return digest.hexdigest()[:32]


class MailSource(Protocol):
    def fetch(self, since: date) -> list[MailMessage]: ...


def _imap_date(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


class _TextExtractor(HTMLParser):
    BREAKS = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _text_body(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    # Truncate BEFORE any processing: bounded work whatever the message size.
    text = (content if isinstance(content, str) else "")[: MAX_BODY_CHARS * 4]
    if part.get_content_type() == "text/html":
        extractor = _TextExtractor()
        extractor.feed(text)
        extractor.close()
        text = "".join(extractor.parts)
    return text[:MAX_BODY_CHARS]


DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_ATTACHMENTS = 3


def docx_attachments(message: EmailMessage) -> tuple[tuple[str, bytes], ...]:
    found: list[tuple[str, bytes]] = []
    for part in message.iter_attachments():
        name = str(part.get_filename() or "")
        if part.get_content_type() != DOCX_TYPE and not name.casefold().endswith(".docx"):
            continue
        content = part.get_payload(decode=True)
        if isinstance(content, bytes) and 0 < len(content) <= MAX_DOCX_BYTES:
            found.append((name[:200] or "plan.docx", content))
        if len(found) >= MAX_ATTACHMENTS:
            break
    return tuple(found)


def authenticated(message: EmailMessage, sender: str, trusted_authserv_id: str) -> bool:
    """True when the user's OWN receiving server recorded, for the sender's domain, DMARC pass
    (``header.from`` aligned) or DKIM pass (``header.d`` / ``header.i``).

    Only the topmost ``Authentication-Results`` header carrying ``trusted_authserv_id`` counts
    (RFC 8601: the receiving server adds it last and removes forged copies of its own id). A
    header written by the sender or by any other server is never trusted, so a forged
    ``From`` with a forged ``dmarc=pass`` is rejected."""
    domain = re.escape(sender.rsplit("@", 1)[-1].casefold())
    trusted = trusted_authserv_id.strip().casefold()
    aligned_dmarc = re.compile(rf"^dmarc=pass\b.*\bheader\.from={domain}(?![\w.-])")
    aligned_dkim = re.compile(rf"^dkim=pass\b.*\bheader\.d={domain}(?![\w.-])")
    aligned_dkim_i = re.compile(rf"^dkim=pass\b.*\bheader\.i=[^\s;]*@{domain}(?![\w.-])")
    for header in message.get_all("Authentication-Results", []):
        parts = [p.strip() for p in str(header).casefold().split(";")]
        if not parts or not parts[0] or parts[0].split()[0] != trusted:
            continue
        return any(
            aligned_dmarc.search(r) or aligned_dkim.search(r) or aligned_dkim_i.search(r)
            for r in parts[1:]
        )  # the topmost trusted header decides; older copies are ignored
    return False


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
        require_authentication: bool = True,
        trusted_authserv_id: str | None = None,
    ) -> None:
        if "@" not in allowed_sender or any(c in allowed_sender for c in '"\\\r\n '):
            raise ValueError("allowed sender must be one plain e-mail address")
        self._authserv = (trusted_authserv_id or "").strip()
        if require_authentication and not self._authserv:
            raise ValueError(
                "the authserv-id of the receiving mail server is required to check DMARC/DKIM"
            )
        self._host, self._port, self._user, self._password = host, port, user, password
        self._sender = allowed_sender.casefold()
        self._mailbox = mailbox
        # Certificate verification is ON by default; only tests inject another context.
        self._ssl = ssl_context or ssl.create_default_context()
        self._require_auth = require_authentication

    @staticmethod
    def _small_enough(imap: imaplib.IMAP4_SSL, uid: str) -> bool:
        status, data = imap.uid("FETCH", uid, "(RFC822.SIZE)")
        first = data[0] if data else None
        raw = first if isinstance(first, bytes) else b""
        match = re.search(rb"RFC822\.SIZE (\d+)", raw)
        return status == "OK" and match is not None and int(match[1]) <= MAX_MESSAGE_BYTES

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
                if not self._small_enough(imap, uid):
                    continue  # too large or unknown size: never downloaded
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
                if self._require_auth and not authenticated(message, sender, self._authserv):
                    logger.warning("training e-mail ignored: sender authentication not proven")
                    continue
                messages.append(
                    MailMessage(
                        str(message.get("Message-ID", f"uid-{uid}")).strip()[:250],
                        sender,
                        str(message.get("Subject", ""))[:300],
                        _text_body(message),
                        docx_attachments(message),
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
    """Process each NEW message once (seen digests are kept), each in its own savepoint so one
    bad message can never block the others."""
    stats = {"trainings": 0, "proposals": 0, "unparsed": 0, "failed": 0}
    seen: list[str] = list(get_setting(session, "training_mail_seen", []) or [])
    known = set(seen)
    for message in messages:
        if message.digest in known:
            continue
        try:
            with session.begin_nested():
                _import_one(session, message, tz, today, interpreter, stats)
        except Exception as exc:  # isolate the message; never log its content
            stats["failed"] += 1
            logger.warning("training e-mail import failed: %s", type(exc).__name__)
        seen.append(message.digest)
        known.add(message.digest)
    set_setting(session, "training_mail_seen", seen[-MAX_SEEN:])
    if stats["trainings"]:
        set_setting(session, "replan_requested", datetime.now(UTC).isoformat())
    return stats


def _import_one(
    session: Session,
    message: MailMessage,
    tz: ZoneInfo,
    today: date,
    interpreter: Interpreter | None,
    stats: dict[str, int],
) -> None:
    for name, content in message.attachments:
        try:
            plan = parse_plan_docx(content, today)
        except PlanFormatError as exc:
            notify(
                session,
                "coach_plan_unreadable",
                "Plan d'entraînement illisible",
                f"La pièce jointe « {name} » n'a pas pu être lue : {exc}. Rien n'a été importé.",
                f"coach_plan_unreadable:{message.digest}:{name}",
            )
            continue
        report = apply_weekly_plan(session, plan, tz=tz, today=today, origin=message.digest)
        stats["trainings"] += len(report.created) + len(report.updated)
    parsed = parse_text(message.body, today)
    direct = ParseResult(activities=[a for a in parsed.activities if a.kind == "training"])
    races = ParseResult(activities=[a for a in parsed.activities if a.kind == "race"])
    applied = apply_result(
        session, direct, tz=tz, today=today, source="email", source_ref=message.digest
    )
    stats["trainings"] += len(applied.created)
    if applied.created:
        notify(
            session,
            "training_added",
            "Entraînements reçus",
            f"Ajouté depuis l'e-mail « {message.subject} » : " + " ; ".join(applied.created),
            f"training_added:{message.digest}",
        )
    if applied.rejected:
        notify(
            session,
            "training_rejected",
            "E-mail d'entraînement : éléments refusés",
            " ; ".join(applied.rejected),
            f"training_rejected:{message.digest}",
        )
    if races.activities:
        _propose(session, message, races, tz, today, "races")
        stats["proposals"] += 1
    leftover = _meaningful(parsed.unparsed)
    if not leftover:
        return
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
            return
    stats["unparsed"] += len(leftover)
    notify(
        session,
        "training_unparsed",
        "E-mail d'entraînement non compris",
        f"{len(leftover)} ligne(s) de « {message.subject} » n'ont pas été reconnues. "
        "Format attendu : « mercredi 16/12 18h30 : fractionné 60 min @club - contenu ».",
        f"training_unparsed:{message.digest}",
    )


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
            "source_ref": f"{message.digest}:{origin}",
            "timezone": tz.key,
            "today": today.isoformat(),
            "subject": message.subject,
            "origin": origin,
        },
        f"training_import:{message.digest}:{origin}",
    )
