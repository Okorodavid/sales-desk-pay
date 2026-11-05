"""The desk: every customer message, owner action and PayPal event goes through here.

It is channel-free. It returns the messages to send (to customers and to the owner) and the channel
code delivers them. The model proposes; the desk decides:
- prices and totals come from the price list, never from the model;
- money is confirmed only by a signature-verified PayPal event (never a screenshot, never the customer's word);
- the PayPal link is made only after the owner approves;
- discounts, bulk orders and complaints always go to the owner;
- while the owner has taken over a chat, the robot stays quiet in it."""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .brain import MEMORY_FIELDS, BrainError, system_prompt
from .config import Settings
from .paypal import PayPalError, VerifiedEvent
from .shop import Shop
from .store import Store, iso, parse

log = logging.getLogger("salesdesk.desk")
STOP = re.compile(r"^\s*(stop|unsubscribe|stop messaging me)\s*\.?\s*$", re.I)
START = re.compile(r"^\s*(start|unstop)\s*$", re.I)
PRICE_TALK = re.compile(r"\b(discount|last price|final price|best price|reduce (it|the price)|too (much|expensive)|bulk|"
                        r"wholesale|negotiat|can you do \d)", re.I)
AMOUNT = re.compile(r"(?:£|\$|€|GBP|USD|EUR)\s?(\d[\d,]*(?:\.\d{1,2})?)|\b(\d[\d,]*(?:\.\d{1,2})?)\s*(?:pounds|quid|gbp)\b", re.I)
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
                 senses=None, paypal=None):
        self.settings, self.store, self.shop, self.brain = settings, store, shop, brain
        self.paypal = paypal  # a PayPalClient (real sandbox or the mock); None = no payment links can be made
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
            return out.extend(self._ask_for_approval(cust, order))

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
            checks.append(f"careful: it shows {self.shop.money(receipt.amount) if receipt.amount else 'no readable amount'}, "
                          f"but {code} is {self.shop.money(order['total'])}")
        if receipt.doubts:
            checks.append(f"Muse noticed: {receipt.doubts}")
        said = "; ".join(checks)
        return (f"{who} sent a receipt: {receipt.line()}. {said[0].upper()}{said[1:]}. "
                "A screenshot is not proof, so the order stays unpaid until PayPal confirms it.")

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
            value = Decimal((m.group(1) or m.group(2)).replace(",", ""))
            if value != value.to_integral_value() or int(value) not in allowed:
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
            lines.append(f"{p.amount_text(it['qty'])}: {self.shop.money(p.price * it['qty'])}")
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
        where = f"{zone.name}: free" if not zone.fee else f"Delivery to {zone.name}: {self.shop.money(zone.fee)}"
        lead = reply if reply and not AMOUNT.search(reply) else ""
        quote = f"{'; '.join(lines)}. {where}. Total: {self.shop.money(total)}. Shall I hold it for you?"
        self._say(out, cust, f"{lead} {quote}".strip())
        return out

    def _ask_for_approval(self, cust, order) -> Outcome:
        """The customer said yes. The robot does not make the PayPal link itself: the owner approves first."""
        out = Outcome()
        self.store.update_order(order["id"], self.now(), stage="approval", pp_note="Waiting for your OK")
        self._say(out, cust, f"Lovely, I'm holding it for you. {self.shop.owner} approves each order first, then I'll send your "
                             f"secure PayPal link for {self.shop.money(order['total'])} right here. It only takes a moment.")
        out.notes.append(OwnerNote(f"{cust['name'] or cust['address']} said yes to {self.code(order['id'])} "
                                   f"({self.shop.money(order['total'])}). Approve it and the robot sends the PayPal link."))
        return out

    def _says_paid(self, cust, order, text: str) -> Outcome:
        """'I've paid.' Words are not proof. This records the claim and points them at the link; only a signed
        PayPal event can mark the order Paid."""
        out = Outcome()
        if not order["total"] or order["stage"] not in ("quoted", "approval", "awaiting_payment"):
            self._handoff(out, cust, "says they paid, but no order is waiting for money", text)
            return out
        self.store.update_order(order["id"], self.now(), says_paid=1, receipt=text[:300])
        if order["stage"] == "awaiting_payment" and order["pp_link"]:
            self._say(out, cust, "Thank you! I can only confirm an order once PayPal itself tells me the money arrived, "
                                 "and I haven't had that message yet. If you haven't paid, this is your link: "
                                 f"{order['pp_link']} . I'll message you here the moment PayPal confirms.")
        else:
            self._say(out, cust, f"Thank you! Your PayPal link isn't ready yet: {self.shop.owner} approves each order first. "
                                 "I'll send it here, and I confirm the order only when PayPal says the money arrived.")
        return out

    def context(self, cust, order, latest: str) -> str:
        mem = self.store.memory(cust)
        known = "\n".join(f"{k}: {v}" for k, v in mem.items() if v) or "nothing yet"
        items = self.store.items(order)
        if order["total"]:
            what = "; ".join(f"{i['qty']} {i['unit']} {i['name']}" for i in items)
            order_text = f"{self.code(order['id'])}, stage {order['stage']}: {what}, total {self.shop.money(order['total'])}"
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
    async def approve_payment(self, order_id: int) -> Outcome:
        """The owner approves the order: make the PayPal link and send it to the customer."""
        out = Outcome()
        order = self.store.get_order(order_id)
        if not order or order["stage"] != "approval" or self.paypal is None:
            return out
        cust = self.store.get_customer(order["customer_id"])
        code = self.code(order_id)
        items = self.store.items(order)
        what = ", ".join(f"{i['qty']} {i['unit']} {i['name']}" for i in items)[:90]
        site = self.settings.site_url
        try:
            pp_id, link = await self.paypal.create_order(order["total"], self.shop.currency, code,
                                                         f"{self.shop.name} {code}: {what}",
                                                         f"{site}/paypal/return", f"{site}/paypal/cancel")
        except PayPalError as exc:
            log.warning("create_order: %s", exc)
            self.store.update_order(order_id, self.now(), pp_note="PayPal link failed: try Approve again")
            out.notes.append(OwnerNote(f"PayPal didn't make the link for {code}: {exc}. Press Approve to try again."))
            return out
        self.store.update_order(order_id, self.now(), stage="awaiting_payment", pp_order_id=pp_id, pp_link=link,
                                pp_note="Link sent, waiting for the buyer")
        self._say(out, cust, f"Here is your secure PayPal link for {self.shop.money(order['total'])} ({code}): {link}\n"
                             "You can pay with PayPal or a card. I'll message you the moment PayPal confirms.", "payment")
        return out

    def buyer_returned(self, pp_order_id: str) -> None:
        """The buyer's browser came back from PayPal. Only a note for the board: it never marks anything Paid."""
        order = self.store.order_by_paypal_id(pp_order_id)
        if order and order["stage"] == "awaiting_payment":
            self.store.update_order(order["id"], self.now(), pp_note="Buyer approved on PayPal, waiting for PayPal's confirmation")

    def paypal_event(self, ev: VerifiedEvent) -> Outcome:
        """The only way an order becomes Paid. `ev` is a VerifiedEvent, which exists only if PayPal itself said the
        message is genuine. Even then the money must match: right order, right amount, right currency, completed."""
        if not isinstance(ev, VerifiedEvent):
            raise TypeError("paypal_event needs a VerifiedEvent")
        out = Outcome()
        now = self.now()
        if self.store.seen_event(ev.event_id):
            return out  # PayPal retries deliveries; a repeat changes nothing
        order = self.store.order_by_paypal_id(ev.pp_order_id)
        oid = order["id"] if order else None

        def log_(outcome: str, note: str) -> Outcome:
            self.store.add_event(now, ev.event_id, ev.event_type, oid, outcome, note)
            return out

        def flag(note: str, order_note: str) -> Outcome:
            self.store.add_event(now, ev.event_id, ev.event_type, oid, "rejected", note)
            if order and order_note:
                self.store.update_order(order["id"], now, pp_note=order_note)
            out.notes.append(OwnerNote(f"PayPal event not used ({ev.event_type}): {note}."))
            return out

        if ev.event_type != "PAYMENT.CAPTURE.COMPLETED":
            return log_("ignored", f"{ev.event_type} is not a completed payment")
        if not order:
            return flag("no order here has that PayPal order id", "")
        code = self.code(order["id"])
        if ev.status != "COMPLETED":
            return flag(f"{code}: PayPal capture status is {ev.status or 'missing'}, not COMPLETED",
                        f"PayPal says {ev.status or '?'}: not paid")
        if ev.currency != self.shop.currency or ev.value is None or ev.value != Decimal(order["total"]):
            got = f"{ev.currency} {ev.value}"
            return flag(f"{code} is {self.shop.currency} {order['total']} but PayPal reports {got}; check it in PayPal",
                        f"PayPal reports {got}: does not match the order")
        if ev.custom_id and ev.custom_id != code:
            return flag(f"{code}: PayPal's reference says {ev.custom_id}", "PayPal reference does not match")
        if order["stage"] in ("paid", "delivered"):
            return log_("duplicate", f"{code} was already paid")
        if order["stage"] != "awaiting_payment":
            return flag(f"{code} was {order['stage']} when the money arrived; refund it in PayPal if it isn't wanted",
                        "Money arrived for an order that wasn't waiting for it")
        self.store.update_order(order["id"], now, stage="paid", says_paid=0, paid_by="paypal", paid_at=iso(now),
                                pp_capture_id=ev.capture_id, pp_note=f"Paid: PayPal capture {ev.capture_id}")
        self.store.add_event(now, ev.event_id, ev.event_type, order["id"], "paid", f"capture {ev.capture_id}")
        for i in self.store.items(order):  # stock goes down once money is in
            p = self.shop.products.get(i["sku"])
            if p:
                p.stock -= i["qty"]
        cust = self.store.get_customer(order["customer_id"])
        first = self.first_name(cust)
        zone = self.shop.zones.get(order["zone"])
        when = f" It goes out {zone.days}." if zone and zone.id != "COLLECT" else " It's ready for collection."
        self._say(out, cust, f"Payment received through PayPal, thank you{' ' + first if first else ''}! "
                             f"Your order {code} is confirmed.{when}", "payment")
        out.notes.append(OwnerNote(f"PayPal confirmed {self.shop.money(order['total'])} for {code} "
                                   f"({cust['name'] or cust['address']}). The customer was told."))
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
                link = order["pp_link"]
                text = (f"{hi}, just checking in. Shall I keep holding your {what}? Your PayPal link: {link}" if link
                        else f"{hi}, just checking in. Shall I keep holding your {what}?")
            self.store.update_order(order["id"], now, follow_ups=order["follow_ups"] + 1)
            self._say(out, cust, text, "follow_up")
        if now.hour >= self.settings.summary_hour and self._summary_day != today:
            self._summary_day = today
            from .views import brief_text

            out.notes.append(OwnerNote(brief_text(self)))
        return out
