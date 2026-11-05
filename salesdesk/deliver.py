"""Sends what the desk decided: each reply by the customer's own channel, owner notes to the owner's chat."""

from __future__ import annotations

import logging
from collections import defaultdict, deque

from .desk import Outbound, Outcome, OwnerNote
from .store import Store

log = logging.getLogger("salesdesk.deliver")


class Dispatcher:
    def __init__(self, store: Store, whatsapp=None, instagram=None, email=None, telegram=None, owner_chat_id: int | None = None):
        self.store = store
        self.whatsapp, self.instagram, self.email, self.telegram = whatsapp, instagram, email, telegram
        self.owner_chat_id = owner_chat_id
        self.demo_inbox: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))  # the demo phone's chats
        self.owner_log: deque = deque(maxlen=100)
        self.owner_count = 0  # notes sent to the owner so far (the demo phone shows only new ones)

    async def deliver(self, outcome: Outcome) -> None:
        for r in outcome.replies:
            try:
                await self._send(r)
            except Exception:  # one failed send must not stop the rest
                log.exception("send failed to %s %s", r.channel, r.address)
        for note in outcome.notes:
            try:
                await self.owner(note)
            except Exception:
                log.exception("owner note failed")

    async def _send(self, r: Outbound) -> None:
        if r.address.startswith("demo-"):
            self.demo_inbox[f"{r.channel}:{r.address}"].append(r.text)
            return
        if r.channel == "whatsapp" and self.whatsapp:
            sent = (await self.whatsapp.send_template(r.address, r.text) if r.outside_window
                    else await self.whatsapp.send(r.address, r.text))
            if sent:
                return
        elif r.channel == "instagram" and self.instagram and not r.outside_window:
            if await self.instagram.send(r.address, r.text):
                return
        elif r.channel == "email" and self.email:
            if await self.email.send(r.address, r.text):
                return
        elif r.channel == "telegram" and self.telegram:
            await self.telegram.send_message(int(r.address), r.text)
            return
        why = "outside the 24-hour window" if r.outside_window else f"{r.channel} sending isn't set up"
        await self.owner(OwnerNote(f"Couldn't send to {r.address} on {r.channel} ({why}). Send it yourself: {r.text}"))

    async def owner(self, note: OwnerNote) -> None:
        self.owner_log.append(note.text)
        self.owner_count += 1
        log.info("OWNER: %s", note.text)
        if self.telegram and self.owner_chat_id:
            msg = await self.telegram.send_message(self.owner_chat_id, note.text)
            if note.handoff_id:
                self.store.set_handoff_ref(note.handoff_id, f"tg:{msg.message_id}")
