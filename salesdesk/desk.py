"""The desk: every customer message, owner action and bank alert goes through here.

It is channel-free. It returns the messages to send (to customers and to the owner) and the channel
code delivers them. The model proposes; the desk decides:
- prices and totals come from the price list, never from the model;
- money is confirmed only by the owner or by a verified bank alert;
- discounts, bulk orders and complaints always go to the owner;
- while the owner has taken over a chat, the robot stays quiet in it."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .brain import MEMORY_FIELDS, BrainError, system_prompt
from .config import Settings
from .payments import Alert, match_order
from .shop import Shop, naira
from .store import Store, iso, parse

log = logging.getLogger("salesdesk.desk")
STOP = re.compile(r"^\s*(stop|unsubscribe|stop messaging me)\s*\.?\s*$", re.I)
START = re.compile(r"^\s*(start|unstop)\s*$", re.I)
PRICE_TALK = re.compile(r"\b(discount|last price|final price|best price|reduce (it|the price)|too (much|expensive)|bulk|"
                        r"wholesale|negotiat|can you do \d)", re.I)
AMOUNT = re.compile(r"(?:₦|NGN|\bN)\s?(\d[\d,]{2,})|\b(\d[\d,]{3,})\s*naira", re.I)
WINDOW = timedelta(hours=24)  # WhatsApp and Instagram allow free replies 24h after the customer's last message


@dataclass
class Outbound:
    customer_id: int
    channel: str
    address: str
    text: str
    kind: str = "reply"  # reply | follow_up | owner | payment
    outside_window: bool = False


@dataclass
class OwnerNote:
    text: str
    handoff_id: int | None = None


@dataclass
class Outcome:
    replies: list[Outbound] = field(default_factory=list)
    notes: list[OwnerNote] = field(default_factory=list)

    def extend(self, other: "Outcome") -> "Outcome":
        self.replies += other.replies
        self.notes += other.notes
        return self


class Desk:
    def __init__(self, settings: Settings, store: Store, shop: Shop, brain, clock: Callable[[], datetime] | None = None,
                 senses=None):
        self.settings, self.store, self.shop, self.brain = settings, store, shop, brain
        self.senses = senses  # voice notes and receipt photos (Meta models); None = treat them as plain placeholders
        self.tz = ZoneInfo(settings.timezone)
        self.clock = clock or (lambda: datetime.now(self.tz))
        self._summary_day = ""

    def now(self) -> datetime:
        return self.clock().astimezone(self.tz)

    def code(self, order_id: int) -> str:
        return f"{self.shop.order_prefix}-{order_id}"

    @staticmethod
    def first_name(cust) -> str:
        return (cust["name"] or "").split(" ")[0] if cust["name"] else ""

    # Sending -----------------------------------------------------------------------------------
    def _say(self, out: Outcome, cust, text: str, kind: str = "reply", who: str = "robot") -> None:
        now = self.now()
        last_in = parse(cust["last_in"])
        outside = cust["channel"] in ("whatsapp", "instagram") and (not last_in or now - last_in > WINDOW)
        self.store.add_message(cust["id"], who, text, now)
        out.replies.append(Outbound(cust["id"], cust["channel"], cust["address"], text, kind, outside))

    def _handoff(self, out: Outcome, cust, reason: str, question: str, reply: str | None = None) -> None:
        hid = self.store.add_handoff(cust["id"], reason, question, self.now())
        who = cust["name"] or cust["address"]
        out.notes.append(OwnerNote(f"{who} ({cust['channel']}) needs you: {reason}.\nThey said: \"{question[:300]}\"\n"
                                   "Reply here, or take over the chat on the order board.", hid))
        if reply is None:
            reply = f"Let me check with {self.shop.owner} and get back to you shortly."
        self._say(out, cust, reply)

    # Customer messages -------------------------------------------------------------------------
    async def handle(self, channel: str, address: str, text: str, name: str = "", ad: dict | None = None) -> Outcome:
        """One customer message. `ad` is the Meta ad that opened the chat, from the webhook's referral:
        {"id", "name", "click"}."""
        out = Outcome()
        now = self.now()
        text = (text or "").strip()[:2000]
        cust = self.store.customer(channel, address, now)
        if name and not cust["name"]:
            self.store.update_customer(cust["id"], name=name.strip()[:60])
        if ad and ad.get("id") and not cust["ad_id"]:
            self.store.set_ad(cust["id"], str(ad["id"]), str(ad.get("name") or ""), str(ad.get("click") or ""), now)
            cust = self.store.get_customer(cust["id"])
        if STOP.match(text):
            self.store.add_message(cust["id"], "customer", text, now)
            self.store.update_customer(cust["id"], opted_out=1)
            cust = self.store.get_customer(cust["id"])
            self._say(out, cust, f"Okay, you won't get more messages from {self.shop.name}. Send START any time to chat again.")
            return out
        if START.match(text) and cust["opted_out"]:
            self.store.update_customer(cust["id"], opted_out=0)
        if not text:
            return out
        self.store.add_message(cust["id"], "customer", text, now)
        cust = self.store.get_customer(cust["id"])
        if cust["opted_out"]:
            return out
        if cust["owner_mode"]:
            out.notes.append(OwnerNote(f"{cust['name'] or cust['address']} ({channel}): {text[:400]}"))
            return out

        order = self.store.open_order(cust["id"]) or self.store.create_order(cust["id"], now)
        if PRICE_TALK.search(text):
            self._handoff(out, cust, "price or bulk request (the robot never changes prices)", text,
                          f"Prices are set by {self.shop.owner} herself, so I've passed this to her. She'll reply you shortly.")
            return out
        pending = self.store.items(order) if not order["total"] else []
        zone = self.shop.zone_for(text)
        if pending and zone:  # they picked something earlier and now said where it goes: quote, no model needed
            mem = self.store.memory(cust)
            mem["area"] = text.strip()[:60] if len(text) <= 60 else zone.name
            self.store.update_customer(cust["id"], memory=mem)
            return out.extend(self._quote(self.store.get_customer(cust["id"]), order,
                                          [{"sku": i["sku"], "qty": i["qty"]} for i in pending], zone.id, text, ""))
        if self.store.bump_calls(now.date().isoformat()) > self.settings.daily_cap:
            self._handoff(out, cust, "daily robot limit reached", text)
            return out
        try:
            turn = (await self.brain.think(system_prompt(self.shop), self.context(cust, order, text))).data
        except BrainError as exc:
            self._handoff(out, cust, f"the robot couldn't answer ({exc})", text)
            return out

        self._remember(cust, turn["memory"])
        cust = self.store.get_customer(cust["id"])
        action = turn["action"]
        reply = turn["reply"]

        if action == "handoff":
            self._handoff(out, cust, turn["handoff_reason"] or "the robot wasn't sure", text, reply or None)
            return out
        if action == "cancel":
            self.store.update_order(order["id"], now, stage="cancelled")
            self._say(out, cust, reply or "No problem, I've cancelled it. Message any time you need anything.")
            return out
        if action == "says_paid":
            return out.extend(self._says_paid(cust, order, text))
        if action == "accept_quote" and order["stage"] == "quoted":
            self.store.update_order(order["id"], now, stage="awaiting_payment")
            self._say(out, cust, f"Great, I'm holding it for you. Please pay {naira(order['total'])} to {self.shop.pay_to}. "
                                 f"Put {self.code(order['id'])} in the transfer description, then send the receipt here.", "payment")
            return out

        items = turn["items"] if action == "quote" else []
        if not items and self.store.items(order) and not order["total"]:
            items = [{"sku": i["sku"], "qty": i["qty"]} for i in self.store.items(order)]  # waiting for a delivery area
        if items:
            return out.extend(self._quote(cust, order, items, turn["zone"], text, reply))

        allowed = self.shop.prices() | {order["total"], order["subtotal"], order["delivery_fee"]}
        if not reply or not self._amounts_ok(reply, allowed):
            self._handoff(out, cust, "the robot wanted to give a price it couldn't check", text)
            return out
        self._say(out, cust, reply)
        return out

    async def handle_media(self, channel: str, address: str, kind: str, data: bytes, name: str = "",
                           ad: dict | None = None, caption: str = "") -> Outcome:
        """A voice note ("audio") or a photo ("image"). Voice notes become text the robot answers. Photos that are
        transfer receipts are read so the owner can check them; they never confirm a payment by themselves."""
        from .meta import MetaError

        ready = self.senses is not None and self.senses.ready
        if kind == "audio":
            heard = ""
            if ready and data:
                try:
                    heard = await self.senses.hear(data, self.shop.keywords())
                except MetaError as exc:
                    log.warning("voice note: %s", exc)
            return await self.handle(channel, address, f"(voice note) {heard}" if heard else "[sent a voice note]", name, ad)
        receipt = None
        if ready and data:
            try:
                receipt = await self.senses.read_receipt(data)
            except MetaError as exc:
                log.warning("photo: %s", exc)
        if receipt is None:
            return await self.handle(channel, address, caption or "[sent a image]", name, ad)
        out = await self.handle(channel, address, f"[sent a receipt: {receipt.line()}] {caption}".strip(), name, ad)
        cust = self.store.customer(channel, address, self.now())
        order = self.store.open_order(cust["id"])
        if order and order["total"]:
            out.notes.append(OwnerNote(self.receipt_note(cust, order, receipt)))
        return out

    def receipt_note(self, cust, order, receipt) -> str:
        code = self.code(order["id"])
        who = cust["name"] or cust["address"]
        checks = []
        if receipt.amount == order["total"]:
            checks.append(f"the amount matches {code}")
        else:
            checks.append(f"careful: it shows {naira(receipt.amount) if receipt.amount else 'no readable amount'}, "
                          f"but {code} is {naira(order['total'])}")
        if code.lower() in receipt.narration.lower().replace(" ", ""):
            checks.append("the order code is in the description")
        if receipt.doubts:
            checks.append(f"Muse noticed: {receipt.doubts}")
        said = "; ".join(checks)
        return (f"{who} sent a receipt: {receipt.line()}. {said[0].upper()}{said[1:]}. "
                "A screenshot isn't proof, so confirm when the money shows in your bank.")

    def _remember(self, cust, updates: dict) -> None:
        mem = self.store.memory(cust)
        changed = False
        for k in MEMORY_FIELDS:
            v = (updates or {}).get(k, "").strip()
            if not v or k == "name":
                continue
            if k == "notes" and mem.get("notes") and v not in mem["notes"]:
                v = (mem["notes"] + "; " + v)[-300:]
            if mem.get(k) != v:
                mem[k], changed = v, True
        if changed:
            self.store.update_customer(cust["id"], memory=mem)
        name = (updates or {}).get("name", "").strip()
        if name and name.lower() not in (cust["name"] or "").lower():
            self.store.update_customer(cust["id"], name=name[:60])

    @staticmethod
    def _amounts_ok(text: str, allowed: set[int]) -> bool:
        for m in AMOUNT.finditer(text):
            if int((m.group(1) or m.group(2)).replace(",", "")) not in allowed:
                return False
        return True

    def _quote(self, cust, order, items: list[dict], zone_id: str, text: str, reply: str) -> Outcome:
        out = Outcome()
        now = self.now()
        lines, chosen, yards = [], [], 0
        for it in items:
            p = self.shop.products.get(it["sku"])
            if not p or it["qty"] <= 0:
                self._handoff(out, cust, f"asked for something not on the price list ({it['sku'] or '?'})", text)
                return out
            if p.stock < it["qty"]:
                self._handoff(out, cust, f"wants {it['qty']} {p.unit}s of {p.name}, only {max(p.stock, 0)} in stock", text,
                              f"We have only {max(p.stock, 0)} {p.unit}s of {p.name} left. Let me ask {self.shop.owner} what we can do.")
                return out
            if p.unit == "yard":
                yards += it["qty"]
            chosen.append({"sku": p.sku, "name": p.name, "unit": p.unit, "qty": it["qty"], "price": p.price})
            lines.append(f"{p.amount_text(it['qty'])}: {naira(p.price * it['qty'])}")
        if yards > self.shop.bulk_over_yards:
            self._handoff(out, cust, f"bulk order, {yards} yards: needs your price", text,
                          f"For {yards} yards {self.shop.owner} gives the price herself. I've passed it to her and she'll reply you shortly.")
            return out
        mem = self.store.memory(cust)
        zone = (self.shop.zones.get(zone_id) or self.shop.zone_for(text) or self.shop.zone_for(mem.get("area", ""))
                or self.shop.zones.get(order["zone"]))
        subtotal = sum(i["price"] * i["qty"] for i in chosen)
        if not zone:
            self.store.update_order(order["id"], now, items=chosen, subtotal=subtotal)
            lead = reply if reply and not AMOUNT.search(reply) else "Nice choice."
            self._say(out, cust, f"{lead} {'; '.join(lines)}. Where should we deliver it? Your area or town is enough.")
            return out
        total = subtotal + zone.fee
        self.store.update_order(order["id"], now, items=chosen, zone=zone.id, subtotal=subtotal,
                                delivery_fee=zone.fee, total=total, stage="quoted", follow_ups=0)
        where = f"{zone.name}: free" if not zone.fee else f"Delivery to {zone.name}: {naira(zone.fee)}"
        lead = reply if reply and not AMOUNT.search(reply) else ""
        quote = f"{'; '.join(lines)}. {where}. Total: {naira(total)}. Shall I hold it for you?"
        self._say(out, cust, f"{lead} {quote}".strip())
        return out

    def _says_paid(self, cust, order, text: str) -> Outcome:
        out = Outcome()
        now = self.now()
        if not order["total"] or order["stage"] not in ("quoted", "awaiting_payment"):
            self._handoff(out, cust, "says they paid, but no order is waiting for money", text)
            return out
        self.store.update_order(order["id"], now, stage="awaiting_payment", says_paid=1, receipt=text[:300])
        for a in self.store.alerts(("unmatched", "to_check")):  # the bank alert may have arrived first
            if a["amount"] == order["total"] and a["verified"]:
                self.store.update_alert(a["id"], order_id=order["id"], status="confirmed", note="matched when the customer said they paid")
                return out.extend(self.confirm_payment(order["id"], by="bank_alert"))
        self._say(out, cust, f"Thank you! {self.shop.owner} is checking the payment now. You'll get a message the moment it's confirmed.")
        out.notes.append(OwnerNote(f"{cust['name'] or cust['address']} says they paid {naira(order['total'])} for "
                                   f"{self.code(order['id'])}. Check your bank, then press \"Money is in\"."))
        return out

    def context(self, cust, order, latest: str) -> str:
        mem = self.store.memory(cust)
        known = "\n".join(f"{k}: {v}" for k, v in mem.items() if v) or "nothing yet"
        items = self.store.items(order)
        if order["total"]:
            what = "; ".join(f"{i['qty']} {i['unit']} {i['name']}" for i in items)
            order_text = f"{self.code(order['id'])}, stage {order['stage']}: {what}, total {naira(order['total'])}"
        elif items:
            order_text = "wants " + "; ".join(f"{i['qty']} {i['unit']} {i['name']}" for i in items) + ", delivery area unknown"
        else:
            order_text = "no order yet"
        zone = self.shop.zone_for(mem.get("area", "")) or self.shop.zones.get(order["zone"])
        hist = self.store.history(cust["id"], self.settings.history_messages + 1)[:-1]
        lines = "\n".join(f"{m['who']}: {m['text']}" for m in hist) or "(first message)"
        return (f"<facts>\n{self.shop.facts()}\n</facts>\n"
                f"<customer>\nname: {cust['name'] or 'unknown'}\nchannel: {cust['channel']}\n{known}\n</customer>\n"
                f"<order>\n{order_text}\n</order>\n<order_stage>{order['stage']}</order_stage>\n"
                f"<zone_hint>{zone.id if zone else ''}</zone_hint>\n"
                f"<history>\n{lines}\n</history>\n<latest_message>\n{latest}\n</latest_message>")

    # The owner's actions -----------------------------------------------------------------------
    def confirm_payment(self, order_id: int, by: str = "owner") -> Outcome:
        out = Outcome()
        order = self.store.get_order(order_id)
        if not order or order["stage"] not in ("quoted", "awaiting_payment"):
            return out
        now = self.now()
        self.store.update_order(order_id, now, stage="paid", says_paid=0, paid_by=by, paid_at=iso(now))
        for i in self.store.items(order):  # stock goes down once money is in
            p = self.shop.products.get(i["sku"])
            if p:
                p.stock -= i["qty"]
        cust = self.store.get_customer(order["customer_id"])
        first = self.first_name(cust)
        zone = self.shop.zones.get(order["zone"])
        when = f" It goes out {zone.days}." if zone and zone.id != "PICKUP" else " It's ready for pickup."
        self._say(out, cust, f"Payment received, thank you{' ' + first if first else ''}! Your order {self.code(order_id)} is confirmed.{when}", "payment")
        if by == "bank_alert":
            out.notes.append(OwnerNote(f"Bank alert matched: {naira(order['total'])} for {self.code(order_id)} "
                                       f"({cust['name'] or cust['address']}). Confirmed, and they were told."))
        return out

    def payment_not_seen(self, order_id: int) -> Outcome:
        out = Outcome()
        order = self.store.get_order(order_id)
        if not order or order["stage"] != "awaiting_payment":
            return out
        self.store.update_order(order_id, self.now(), says_paid=0)
        cust = self.store.get_customer(order["customer_id"])
        self._say(out, cust, "Thanks for waiting. We can't see the transfer yet. Transfers sometimes take a few minutes, "
                             "so we'll confirm as soon as it lands. Could you check it went to the right account?")
        return out

    def mark_delivered(self, order_id: int) -> Outcome:
        out = Outcome()
        order = self.store.get_order(order_id)
        if not order or order["stage"] != "paid":
            return out
        now = self.now()
        self.store.update_order(order_id, now, stage="delivered", delivered_at=iso(now))
        cust = self.store.get_customer(order["customer_id"])
        self._say(out, cust, f"Your order is on its way to you. Thank you for buying from {self.shop.name}! "
                             "Send us a photo when you've sewn it, we'd love to see.")
        return out

    def owner_reply(self, customer_id: int, text: str) -> Outcome:
        """The owner writes to the customer. That takes over the chat until she gives it back."""
        out = Outcome()
        cust = self.store.get_customer(customer_id)
        if not cust or cust["opted_out"] or not text.strip():
            return out
        self.store.update_customer(customer_id, owner_mode=1)
        self._say(out, self.store.get_customer(customer_id), text.strip()[:2000], "owner", who="owner")
        return out

    def set_takeover(self, customer_id: int, on: bool) -> None:
        self.store.update_customer(customer_id, owner_mode=1 if on else 0)
        if not on:
            self.store.close_handoffs(customer_id)

    def robot_handles(self, customer_id: int) -> None:
        """'Let the robot handle it': close what was waiting on the owner and give the chat back."""
        self.set_takeover(customer_id, False)

    def skip_follow_up(self, order_id: int, skip: bool = True) -> None:
        self.store.update_order(order_id, self.now(), skip_day=self.now().date().isoformat() if skip else "")

    # Bank alerts -------------------------------------------------------------------------------
    def bank_alert(self, alert: Alert) -> Outcome:
        out = Outcome()
        if self.store.seen_alert(alert.msg_id):
            return out
        waiting = []
        for o in self.store.waiting_for_money():
            c = self.store.get_customer(o["customer_id"])
            waiting.append({"id": o["id"], "total": o["total"], "name": c["name"] or "", "says_paid": o["says_paid"]})
        matches, how = match_order(alert, waiting, self.shop.order_prefix)
        fields = dict(msg_id=alert.msg_id, amount=alert.amount, sender=alert.sender, narration=alert.narration,
                      bank=alert.bank, verified=int(alert.verified))
        who = alert.sender or "someone"
        if not matches:
            self.store.add_alert(self.now(), status="unmatched", note=how, **fields)
            out.notes.append(OwnerNote(f"Money in: {naira(alert.amount)} from {who}, but no order is waiting for that amount."
                                       + ("" if alert.verified else f" Careful: {alert.why}.")))
            return out
        sure = len(matches) == 1 and (how != "amount only" or matches[0]["says_paid"])
        if alert.verified and sure and self.settings.auto_confirm:
            self.store.add_alert(self.now(), status="confirmed", order_id=matches[0]["id"], note=f"{how}; {alert.why}", **fields)
            return out.extend(self.confirm_payment(matches[0]["id"], by="bank_alert"))
        note = alert.why if not alert.verified else ("more than one order fits" if len(matches) > 1 else f"matched by {how}")
        self.store.add_alert(self.now(), status="to_check", order_id=matches[0]["id"], note=note, **fields)
        out.notes.append(OwnerNote(f"Check this alert: {naira(alert.amount)} from {who} looks like {self.code(matches[0]['id'])} "
                                   f"({matches[0]['name'] or 'customer'}). Not confirmed by itself because {note}."))
        return out

    def alert_confirm(self, alert_id: int, order_id: int | None = None) -> Outcome:
        a = self.store.get_alert(alert_id)
        if not a:
            return Outcome()
        oid = order_id or a["order_id"]
        self.store.update_alert(alert_id, status="owner_confirmed", order_id=oid)
        return self.confirm_payment(oid, by="owner") if oid else Outcome()

    def alert_dismiss(self, alert_id: int) -> None:
        self.store.update_alert(alert_id, status="dismissed")

    # The clock: follow-ups and the morning summary ---------------------------------------------
    def follow_up_due(self, order) -> datetime | None:
        if order["stage"] not in ("quoted", "awaiting_payment") or order["says_paid"]:
            return None
        if order["follow_ups"] >= self.shop.max_follow_ups:
            return None
        cust = self.store.get_customer(order["customer_id"])
        last_in = parse(cust["last_in"]) if cust else None
        if not cust or not last_in or cust["opted_out"] or cust["owner_mode"]:
            return None
        return (last_in + timedelta(hours=self.shop.follow_up_hours * (order["follow_ups"] + 1))).astimezone(self.tz)

    def tick(self) -> Outcome:
        out = Outcome()
        now = self.now()
        today = now.date().isoformat()
        for order in self.store.orders(("quoted", "awaiting_payment")):
            due = self.follow_up_due(order)
            if not due or due > now or order["skip_day"] == today:
                continue
            cust = self.store.get_customer(order["customer_id"])
            items = self.store.items(order)
            what = items[0]["name"] if items else "your order"
            first = self.first_name(cust)
            hi = f"Hi {first}" if first else "Hi"
            if order["stage"] == "quoted":
                text = f"{hi}, still interested in the {what}? I can hold it for you today."
            else:
                text = f"{hi}, just checking in. Shall I keep holding your {what}? Send the receipt here once you pay."
            self.store.update_order(order["id"], now, follow_ups=order["follow_ups"] + 1)
            self._say(out, cust, text, "follow_up")
        if now.hour >= self.settings.summary_hour and self._summary_day != today:
            self._summary_day = today
            from .views import brief_text

            out.notes.append(OwnerNote(brief_text(self)))
        return out
