"""Bank alerts: read credit-alert emails, check they really came from the bank, and match them to orders.

Fake credit alerts are a common scam, so an alert counts as verified only when ALL hold:
- the From address is on the shop's bank list (BANK_ALERT_SENDERS);
- the receiving mail server recorded dkim=pass or dmarc=pass for that same domain
  (the Authentication-Results header Gmail and most providers add).
Anything else is shown to the owner to check by hand; it never confirms an order by itself."""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parseaddr

log = logging.getLogger("salesdesk.payments")

AMOUNT = re.compile(
    r"(?:amount|credited|credit|sum of|received|value)[^\d₦N]{0,40}(?:₦|NGN|N)\s?([\d,]+(?:\.\d{1,2})?)"
    r"|(?:₦|NGN)\s?([\d,]+(?:\.\d{1,2})?)\s*(?:has been|was|is)?\s*(?:credited|received|paid into)", re.I)
DEBIT = re.compile(r"\b(debit alert|debited|withdrawal|you sent|transfer to)\b", re.I)
CREDIT = re.compile(r"\b(credit alert|credited|money in|received|inflow|you have received|credit transaction)\b", re.I)
FIELD = r"(?:^|\n|\|)\s*(?:{labels})\s*\|?\s*[:\-]\s*\|?\s*([^\n|]+)"
SENDER_LABELS = "sender(?: name)?|from|originator|remitter|sender's name|payer"
NARRATION_LABELS = "narration|description|remarks?|details|reference|memo|purpose"


@dataclass
class Alert:
    msg_id: str
    amount: int
    sender: str
    narration: str
    bank: str
    verified: bool
    why: str  # why it is or isn't verified, shown to the owner


def _domain(addr: str) -> str:
    return parseaddr(addr)[1].rpartition("@")[2].lower()


def _on_list(domain: str, allowed: tuple[str, ...]) -> str:
    """The allowed domain this sender belongs to ('' if none). alerts.gtbank.com belongs to gtbank.com."""
    for d in allowed:
        if domain == d or domain.endswith("." + d):
            return d
    return ""


def _auth_pass(auth_results: str, domain: str) -> bool:
    """dkim=pass for header.d/header.i of that domain, or dmarc=pass with header.from of that domain."""
    low = (auth_results or "").lower()
    for m in re.finditer(r"dkim=pass[^;]*?header\.(?:d|i)=@?([a-z0-9.\-]+)", low):
        if m.group(1) == domain or m.group(1).endswith("." + domain):
            return True
    for m in re.finditer(r"dmarc=pass[^;]*?header\.from=([a-z0-9.\-]+)", low):
        if m.group(1) == domain or m.group(1).endswith("." + domain):
            return True
    return False


def _text(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    body = part.get_content()
    if part.get_content_type() == "text/html":
        body = re.sub(r"<(br|/p|/tr|/div)[^>]*>", "\n", body, flags=re.I)
        body = re.sub(r"</t[dh]>", " | ", body, flags=re.I)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"&nbsp;", " ", body)
        body = re.sub(r"&amp;", "&", body)
    return re.sub(r"[ \t]+", " ", body)


def parse_alert(raw: bytes, allowed: tuple[str, ...]) -> Alert | None:
    """A credit alert from a raw email (RFC 822), or None if it is not a credit alert."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    subject = str(msg.get("Subject", ""))
    body = _text(msg)
    whole = subject + "\n" + body
    if not CREDIT.search(whole) or (DEBIT.search(subject) and not CREDIT.search(subject)):
        return None
    m = AMOUNT.search(body) or AMOUNT.search(subject)  # the body's figure wins over a shortened subject
    if not m:
        return None
    amount = int(float((m.group(1) or m.group(2)).replace(",", "")))
    if amount <= 0:
        return None
    sender = _field(body, SENDER_LABELS)
    narration = _field(body, NARRATION_LABELS)
    from_domain = _domain(str(msg.get("From", "")))
    bank = _on_list(from_domain, allowed)
    auths = " ; ".join(str(h) for h in (msg.get_all("Authentication-Results") or []))
    if not bank:
        verified, why = False, f"sent from {from_domain or 'an unknown address'}, which is not on your bank list"
    elif not _auth_pass(auths, from_domain):
        verified, why = False, f"says it is from {from_domain} but the email's signature check did not pass"
    else:
        verified, why = True, f"signed by {from_domain}"
    msg_id = str(msg.get("Message-ID", "")).strip() or f"{from_domain}:{amount}:{subject[:40]}"
    return Alert(msg_id, amount, sender, narration, bank or from_domain, verified, why)


def _field(body: str, labels: str) -> str:
    m = re.search(FIELD.format(labels=labels), body, re.I)
    return " ".join(m.group(1).split())[:120] if m else ""


def _tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]+", (name or "").lower()) if len(t) > 2}


def match_order(alert: Alert, orders: list[dict], prefix: str) -> tuple[list[dict], str]:
    """Orders this payment could be for, and how they matched. Each order dict has id, total, name.

    Exact amount first; then the order code in the narration (e.g. NF-12), then the sender's name."""
    same = [o for o in orders if o["total"] == alert.amount]
    if not same:
        return [], "no order waiting for that amount"
    code = re.findall(re.escape(prefix) + r"\s*-?\s*(\d+)", (alert.narration + " " + alert.sender).upper())
    if code:
        by_code = [o for o in same if str(o["id"]) in code]
        if by_code:
            return by_code, "amount and order code"
    words = _tokens(alert.sender) | _tokens(alert.narration)
    by_name = [o for o in same if _tokens(o["name"]) & words]
    if by_name:
        return by_name, "amount and name"
    return same, "amount only"


class MailboxReader:
    """Reads new credit alerts from an IMAP mailbox (Gmail: imap.gmail.com with an app password)."""

    def __init__(self, host: str, user: str, password: str, folder: str, allowed: tuple[str, ...]):
        self.host, self.user, self.password, self.folder, self.allowed = host, user, password, folder, allowed

    @property
    def ready(self) -> bool:
        return bool(self.host and self.user and self.password)

    def fetch(self, since: datetime) -> list[bytes]:
        """Raw emails from bank domains received since `since` (read-only; nothing is marked or moved)."""
        out = []
        day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
        with imaplib.IMAP4_SSL(self.host) as box:
            box.login(self.user, self.password)
            box.select(self.folder, readonly=True)
            for domain in self.allowed:
                status, data = box.search(None, "SINCE", day, "FROM", f'"{domain}"')
                if status != "OK":
                    continue
                for num in (data[0] or b"").split()[-50:]:
                    status, parts = box.fetch(num, "(BODY.PEEK[])")
                    if status == "OK" and parts and isinstance(parts[0], tuple):
                        out.append(parts[0][1])
        return out
