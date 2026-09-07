"""IMAP email checker for Mail.ru."""

from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path
from typing import Sequence

from app.config import settings

log = logging.getLogger(__name__)


def _decode_header_value(raw: str) -> str:
    """Decode RFC 2047 encoded header value."""
    parts = decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return "".join(decoded)


def _extract_order_code(text: str) -> str | None:
    """Extract order code from text. Supports both dash and no-dash formats."""
    # Try with dash first (e.g. МКП-1079)
    match = re.search(r"([А-ЯA-Z]{1,5}-\d{3,5})", text)
    if match:
        return match.group(1)
    # Try without dash (e.g. МКП1079)
    match = re.search(r"([А-ЯA-Z]{2,5}\d{3,5})", text)
    if match:
        return match.group(1)
    return None


class EmailChecker:
    """Periodic IMAP checker for order-related emails."""

    def __init__(self) -> None:
        self.host = settings.imap_host
        self.port = settings.imap_port
        self.user = settings.imap_user
        self.password = settings.imap_password

    def _connect(self) -> imaplib.IMAP4_SSL:
        """Connect to IMAP server."""
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        conn.login(self.user, self.password)
        return conn

    def check_new_emails(
        self,
        since_days: int = 7,
        folder: str = "INBOX",
    ) -> list[dict]:
        """Check for new emails with order codes in subject.

        Returns list of dicts with: subject, order_code, date, uid, from
        """
        log.info(f"[email] Checking IMAP {self.host}:{self.port} folder={folder} since={since_days}d")
        conn = self._connect()
        try:
            conn.select(folder)
            since_date = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
            _, msg_ids = conn.search(None, f'(SINCE "{since_date}")')

            if not msg_ids[0]:
                return []

            results = []
            for uid in msg_ids[0].split():
                uid_str = uid.decode()
                _, msg_data = conn.fetch(uid_str, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                subject = _decode_header_value(msg.get("Subject", ""))
                from_addr = msg.get("From", "")
                date_str = msg.get("Date", "")

                order_code = _extract_order_code(subject)
                if order_code:
                    log.info(f"[email] Found order code in subject: {order_code} <- '{subject}'")
                    results.append({
                        "subject": subject,
                        "order_code": order_code,
                        "date": date_str,
                        "uid": uid_str,
                        "from": from_addr,
                    })

            log.info(f"[email] Scan complete: {len(results)} emails with order codes found")
            return results
        finally:
            conn.close()
            conn.logout()

    def download_attachments(
        self,
        uid: str,
        save_dir: Path | None = None,
        folder: str = "INBOX",
    ) -> list[tuple[str, bytes]]:
        """Download all attachments from a specific email.

        Returns list of (filename, data) pairs.
        If save_dir is provided, also writes files to disk.
        """
        conn = self._connect()
        try:
            conn.select(folder)
            _, msg_data = conn.fetch(uid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                return []

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            attachments: list[tuple[str, bytes]] = []
            for part in msg.walk():
                content_disposition = part.get("Content-Disposition", "")
                if "attachment" not in content_disposition:
                    continue

                filename = part.get_filename()
                if not filename:
                    continue

                # Decode filename
                filename = _decode_header_value(filename)
                data = part.get_payload(decode=True)
                if data:
                    attachments.append((filename, data))
                    if save_dir:
                        save_dir.mkdir(parents=True, exist_ok=True)
                        (save_dir / filename).write_bytes(data)

            return attachments
        finally:
            conn.close()
            conn.logout()

    def find_and_download_by_order_code(
        self,
        order_code: str,
        save_dir: Path | None = None,
        since_days: int = 30,
    ) -> list[tuple[str, bytes]]:
        """Find email by order code in subject and download its attachments.

        Tries exact match first, then dash-insensitive match.
        """
        log.info(f"[email] Searching for order_code={order_code} in email subjects (since {since_days}d)")
        emails = self.check_new_emails(since_days=since_days)

        # Exact match
        for mail in emails:
            if mail["order_code"] == order_code:
                log.info(f"[email] Exact match: code={order_code} uid={mail['uid']} subject='{mail['subject']}'")
                return self.download_attachments(mail["uid"], save_dir)

        # Dash-insensitive match (МКП-1079 == МКП1079)
        code_no_dash = order_code.replace("-", "")
        for mail in emails:
            mail_code_no_dash = mail["order_code"].replace("-", "")
            if mail_code_no_dash == code_no_dash and mail_code_no_dash != mail["order_code"]:
                log.info(f"[email] Dash-insensitive match: {order_code} ~= {mail['order_code']} uid={mail['uid']}")
                return self.download_attachments(mail["uid"], save_dir)

        log.warning(f"[email] No email found for order_code={order_code}")
        return []


async def fetch_email_attachments(order_code: str, save_dir: Path | None = None) -> list[tuple[str, bytes]]:
    """Async wrapper for email attachment fetch."""
    import asyncio
    checker = EmailChecker()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: checker.find_and_download_by_order_code(order_code, save_dir)
    )
