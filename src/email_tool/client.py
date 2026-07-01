"""IMAP-клиент mail.ru. СТРОГО только чтение.

Ящик открывается через select(readonly=True); используются только команды
SEARCH и FETCH. Операции записи (STORE, APPEND, EXPUNGE, COPY) и отправка
писем не реализованы намеренно — см. specs/master-spec.md, раздел 4.
"""
import email
import email.header
import email.utils
import imaplib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes


@dataclass(frozen=True)
class EmailMessage:
    uid: str
    sender_name: str
    sender_email: str
    subject: str
    date: datetime
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)


def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    parts = []
    for value, charset in email.header.decode_header(raw):
        if isinstance(value, bytes):
            parts.append(value.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(value)
    return "".join(parts)


def _extract_body_text(msg: email.message.Message) -> str:
    """Возвращает текстовую часть письма (text/plain приоритетнее text/html)."""
    plain, html_part = [], []
    for part in msg.walk():
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if part.get_content_subtype() == "plain":
            plain.append(text)
        elif part.get_content_subtype() == "html":
            html_part.append(text)
    if plain:
        return "\n".join(plain)
    if html_part:
        # Грубое удаление тегов; для подписи этого достаточно
        import re

        return re.sub(r"<[^>]+>", " ", "\n".join(html_part))
    return ""


def _extract_attachments(msg: email.message.Message) -> list[Attachment]:
    result = []
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        result.append(Attachment(filename=_decode_header(filename), content=payload))
    return result


class MailClient:
    """Синхронный read-only клиент; вызывать через asyncio.to_thread."""

    def __init__(self, host: str, port: int, user: str, password: str) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self._host, self._port)
        conn.login(self._user, self._password)
        conn.select("INBOX", readonly=True)
        return conn

    def search(
        self,
        sender: str | None = None,
        subject: str | None = None,
        text: str | None = None,
        since: date | None = None,
        limit: int = 50,
    ) -> list[EmailMessage]:
        """Поиск писем. Возвращает письма без вложений (заголовки + текст),
        отсортированные от новых к старым."""
        criteria: list[str] = []
        if sender:
            criteria += ["FROM", f'"{sender}"']
        if subject:
            criteria += ["SUBJECT", f'"{subject}"']
        if text:
            criteria += ["TEXT", f'"{text}"']
        if since:
            criteria += ["SINCE", since.strftime("%d-%b-%Y")]
        if not criteria:
            criteria = ["ALL"]

        conn = self._connect()
        try:
            # CHARSET UTF-8 — для поиска кириллицы в теме/тексте
            status, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *
                                    [c.encode() if isinstance(c, str) else c for c in criteria])
            if status != "OK":
                raise RuntimeError(f"IMAP SEARCH failed: {status}")
            uids = data[0].split()
            uids = uids[-limit:][::-1]  # последние limit, от новых к старым
            return [self._fetch(conn, uid, with_attachments=False) for uid in uids]
        finally:
            conn.logout()

    def fetch_message(self, uid: str) -> EmailMessage:
        """Полное письмо с вложениями по UID."""
        conn = self._connect()
        try:
            return self._fetch(conn, uid.encode(), with_attachments=True)
        finally:
            conn.logout()

    def _fetch(
        self, conn: imaplib.IMAP4_SSL, uid: bytes, with_attachments: bool
    ) -> EmailMessage:
        # BODY.PEEK не ставит флаг \Seen — ящик остаётся нетронутым
        status, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK" or not data or data[0] is None:
            raise RuntimeError(f"IMAP FETCH failed for uid={uid!r}: {status}")
        msg = email.message_from_bytes(data[0][1])

        sender_name, sender_email = email.utils.parseaddr(msg.get("From", ""))
        msg_date = email.utils.parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else datetime.min

        return EmailMessage(
            uid=uid.decode(),
            sender_name=_decode_header(sender_name),
            sender_email=sender_email,
            subject=_decode_header(msg.get("Subject")),
            date=msg_date,
            body_text=_extract_body_text(msg),
            attachments=_extract_attachments(msg) if with_attachments else [],
        )
