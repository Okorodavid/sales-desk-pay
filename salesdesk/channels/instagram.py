"""Instagram DMs through the Instagram API with Instagram Login (a professional account linked to a Meta app).

Webhooks arrive with object "instagram" and the same X-Hub-Signature-256 as WhatsApp. Replies are free
within 24 hours of the customer's last message. Not yet run against a live account."""

from __future__ import annotations

import logging

import httpx

from .whatsapp import valid_signature  # same Meta signature scheme

log = logging.getLogger("salesdesk.instagram")

__all__ = ["valid_signature", "incoming", "InstagramSender"]


def ad_from(ev: dict) -> dict | None:
    """The click-to-Instagram-Direct ad behind a message: a referral with source ADS, on the event or the message."""
    ref = ev.get("referral") or (ev.get("message") or {}).get("referral") or {}
    if str(ref.get("source", "")).upper() != "ADS" or not ref.get("ad_id"):
        return None
    ctx = ref.get("ads_context_data") or {}
    return {"id": str(ref["ad_id"]), "name": ctx.get("ad_title") or "", "click": ref.get("ref") or ""}


def incoming(payload: dict, own_id: str = "") -> list[tuple[str, str, dict | None]]:
    """(sender id, text, ad) for each customer message in a webhook payload. Echoes of our own replies are skipped."""
    out = []
    if payload.get("object") != "instagram":
        return out
    for entry in payload.get("entry", []):
        for ev in entry.get("messaging", []) or []:
            msg = ev.get("message") or {}
            sender = (ev.get("sender") or {}).get("id", "")
            if msg.get("is_echo") or not sender or sender == own_id:
                continue
            text = msg.get("text", "")
            if not text and msg.get("attachments"):
                kind = (msg["attachments"][0] or {}).get("type", "file")
                text = f"[sent a {'image' if kind == 'image' else kind}]"
            if text.strip():
                out.append((sender, text.strip(), ad_from(ev)))
    return out


class InstagramSender:
    def __init__(self, token: str, user_id: str, client: httpx.AsyncClient | None = None):
        self.token, self.user_id = token, user_id
        self.http = client or httpx.AsyncClient(timeout=20)

    @property
    def ready(self) -> bool:
        return bool(self.token and self.user_id)

    async def send(self, to: str, text: str) -> bool:
        r = await self.http.post(f"https://graph.instagram.com/v23.0/{self.user_id}/messages",
                                 json={"recipient": {"id": to}, "message": {"text": text[:1000]}},
                                 headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code >= 300:
            log.warning("Instagram send failed %s: %s", r.status_code, r.text[:300])
            return False
        return True

    async def aclose(self) -> None:
        await self.http.aclose()
