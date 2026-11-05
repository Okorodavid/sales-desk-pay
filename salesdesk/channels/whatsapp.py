"""WhatsApp through Meta's Cloud API (adapted from CheckAm).

Free-form replies are allowed within 24 hours of the customer's last message. Follow-ups after that
need an approved template (WHATSAPP_TEMPLATE with one body variable)."""

from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

log = logging.getLogger("salesdesk.whatsapp")


def valid_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not app_secret:
        return True  # checks are on once WHATSAPP_APP_SECRET is set
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def ad_from(m: dict) -> dict | None:
    """The click-to-WhatsApp ad behind a message, from its referral object (sent on the first message only)."""
    ref = m.get("referral") or {}
    if not ref.get("source_id") or (ref.get("source_type") or "ad").lower() != "ad":
        return None
    return {"id": str(ref["source_id"]), "name": ref.get("headline") or "", "click": ref.get("ctwa_clid") or ""}


def incoming(payload: dict) -> list[tuple[str, str, str, str, dict]]:
    """(sender phone, profile name, message id, text, extra) for each message in a webhook payload.
    extra: {"ad": the ad that opened the chat or None, "media": {"id", "kind", "mime"} for voice notes and photos}."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts", []) or []}
            for m in value.get("messages", []) or []:
                kind = m.get("type")
                text, media = "", None
                if kind == "text":
                    text = m.get("text", {}).get("body", "")
                elif kind == "button":
                    text = m.get("button", {}).get("text", "")
                elif kind == "interactive":
                    i = m.get("interactive", {})
                    text = (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
                elif kind in ("image", "video", "document"):
                    text = m.get(kind, {}).get("caption", "") or f"[sent a {kind}]"
                    if kind == "image":
                        media = {"id": m["image"].get("id", ""), "kind": "image", "mime": m["image"].get("mime_type", "")}
                elif kind == "audio":
                    text = "[sent a voice note]"
                    media = {"id": m.get("audio", {}).get("id", ""), "kind": "audio", "mime": m.get("audio", {}).get("mime_type", "")}
                if text.strip():
                    sender = m.get("from", "")
                    out.append((sender, names.get(sender, ""), m.get("id", ""), text.strip(), {"ad": ad_from(m), "media": media}))
    return out


class WhatsAppSender:
    def __init__(self, token: str, phone_number_id: str, graph_version: str = "v23.0", template: str = "",
                 client: httpx.AsyncClient | None = None):
        self.token, self.phone_id, self.version, self.template = token, phone_number_id, graph_version, template
        self.http = client or httpx.AsyncClient(timeout=20)

    @property
    def ready(self) -> bool:
        return bool(self.token and self.phone_id)

    def _url(self) -> str:
        return f"https://graph.facebook.com/{self.version}/{self.phone_id}/messages"

    async def _post(self, body: dict) -> bool:
        r = await self.http.post(self._url(), json=body, headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code >= 300:
            log.warning("WhatsApp send failed %s: %s", r.status_code, r.text[:300])
            return False
        return True

    async def send(self, to: str, text: str) -> bool:
        return await self._post({"messaging_product": "whatsapp", "to": to, "type": "text",
                                 "text": {"body": text[:4000], "preview_url": False}})

    async def send_template(self, to: str, text: str) -> bool:
        if not self.template:
            return False
        return await self._post({
            "messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {"name": self.template, "language": {"code": "en"},
                         "components": [{"type": "body", "parameters": [{"type": "text", "text": text[:1000]}]}]},
        })

    async def download(self, media_id: str) -> bytes:
        """A customer's voice note or photo: look up its URL, then fetch it with the same token."""
        auth = {"Authorization": f"Bearer {self.token}"}
        r = await self.http.get(f"https://graph.facebook.com/{self.version}/{media_id}", headers=auth)
        r.raise_for_status()
        f = await self.http.get(r.json()["url"], headers=auth)
        f.raise_for_status()
        return f.content

    async def aclose(self) -> None:
        await self.http.aclose()
