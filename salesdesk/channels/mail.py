"""Email: customer emails arrive at /inbound/email from a forwarder; replies go out over SMTP."""

from __future__ import annotations

import asyncio
import email
import email.policy
import logging
import re
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr

log = logging.getLogger("salesdesk.mail")


def parse_inbound(raw: bytes) -> tuple[str, str, str]:
    """(address, display name, text) from a raw email. Quoted earlier replies are cut off."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = parseaddr(str(msg.get("From", "")))
    part = msg.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part else ""
    if part is not None and part.get_content_type() == "text/html":
        body = re.sub(r"<[^>]+>", " ", body)
    body = re.split(r"\n\s*(?:On .{5,80} wrote:|-----Original Message-----|From: )", body)[0]
    subject = str(msg.get("Subject", "")).strip()
    text = " ".join(body.split())
    if subject and not subject.lower().startswith("re:"):
        text = f"{subject}. {text}" if text else subject
    return addr.lower(), name, text[:2000]


class EmailSender:
    def __init__(self, host: str, port: int, user: str, password: str, sender: str, shop_name: str):
        self.host, self.port, self.user, self.password, self.sender, self.shop_name = host, port, user, password, sender, shop_name

    @property
    def ready(self) -> bool:
        return bool(self.host and self.user and self.password and self.sender)

    def _send(self, to: str, text: str) -> None:
        msg = EmailMessage()
        msg["From"] = f"{self.shop_name} <{self.sender}>"
        msg["To"] = to
        msg["Subject"] = f"Your message to {self.shop_name}"
        msg.set_content(text)
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            s.starttls()
            s.login(self.user, self.password)
            s.send_message(msg)

    async def send(self, to: str, text: str) -> bool:
        try:
            await asyncio.to_thread(self._send, to, text)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("email to %s failed: %s", to, exc)
            return False
