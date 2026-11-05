"""What the owner's screens show: the morning brief, the order board and one chat."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .store import parse

CHANNEL_NAMES = {"whatsapp": "WhatsApp", "instagram": "Instagram", "tiktok": "TikTok", "email": "Email",
                 "telegram": "Telegram", "web": "Web chat"}
STAGE_LABELS = {"asking": "Asking", "quoted": "Price sent", "approval": "Needs your OK", "awaiting_payment": "Awaiting PayPal",
                "paid": "Paid", "delivered": "Delivered"}


def _since_yesterday(desk) -> datetime:
    return datetime.combine(desk.now().date() - timedelta(days=1), time(0), desk.tz)


def _overnight(desk) -> datetime:
    return datetime.combine(desk.now().date() - timedelta(days=1), time(18), desk.tz)


def _when(desk, ts: str) -> str:
    dt = parse(ts)
    if not dt:
        return ""
    local = dt.astimezone(desk.tz)
    day = "" if local.date() == desk.now().date() else ("yesterday " if local.date() == desk.now().date() - timedelta(days=1) else local.strftime("%d %b "))
    return day + local.strftime("%I:%M %p").lstrip("0").lower()


def _who(cust) -> str:
    return cust["name"] or cust["address"]


def _what(desk, order) -> str:
    items = desk.store.items(order)
    if not items:
        return "Asking"
    return "; ".join(f"{i['qty']} {i['unit']}{'s' if i['qty'] != 1 else ''} {i['name']}" for i in items)


def next_step(desk, order, cust) -> tuple[str, bool]:
    """The robot's next step for an order, and whether it is waiting on the owner."""
    if cust["owner_mode"]:
        return "You took over this chat", True
    if any(h["customer_id"] == cust["id"] for h in desk.store.open_handoffs()):
        return "Needs you", True
    stage = order["stage"]
    if stage == "approval":
        return "Approve the PayPal link", True
    if stage == "awaiting_payment":
        return ("Says paid. Not proof: waiting for PayPal" if order["says_paid"] else "Waiting for PayPal to confirm"), False
    if stage == "paid":
        return "Send it, then mark delivered", True
    if stage == "delivered":
        return "Done", False
    due = desk.follow_up_due(order)
    if due:
        return f"Robot follows up {_when(desk, due.isoformat())}", False
    if stage == "quoted":
        return "Robot is waiting for them", False
    return "Robot is answering", False


def paid_since(desk, since: datetime) -> list:
    return [o for o in desk.store.orders(("paid", "delivered")) if o["paid_at"] and parse(o["paid_at"]) >= since]


def _mode(desk) -> str:
    return "mock" if desk.settings.paypal_mock else desk.settings.paypal_env


def brief(desk) -> dict:
    s, shop = desk.store, desk.shop
    money = shop.money
    now = desk.now()
    paid = paid_since(desk, _since_yesterday(desk))
    chats = s.inbound_since(_overnight(desk))
    needs = []
    for o in s.orders(("approval",)):
        c = s.get_customer(o["customer_id"])
        needs.append({"kind": "approval", "order_id": o["id"], "customer_id": c["id"],
                      "channel": CHANNEL_NAMES.get(c["channel"], c["channel"]), "when": _when(desk, o["updated"]),
                      "title": f"{_who(c)} said yes: {money(o['total'])}",
                      "detail": f"{desk.code(o['id'])}: {_what(desk, o)}. Approve it and the robot sends the PayPal link."})
    for h in s.open_handoffs():
        c = s.get_customer(h["customer_id"])
        needs.append({"kind": "handoff", "customer_id": c["id"], "channel": CHANNEL_NAMES.get(c["channel"], c["channel"]),
                      "when": _when(desk, h["created"]), "title": f"{_who(c)}: {h['reason']}",
                      "detail": f"They said: \"{h['question'][:220]}\""})
    end_of_day = datetime.combine(now.date(), time(23, 59), desk.tz)
    follows = []
    for o in s.orders(("quoted", "awaiting_payment")):
        due = desk.follow_up_due(o)
        if due and due <= end_of_day:
            c = s.get_customer(o["customer_id"])
            follows.append({"order_id": o["id"], "time": _when(desk, due.isoformat()) if due > now else "now",
                            "name": _who(c), "about": _what(desk, o), "skipped": o["skip_day"] == now.date().isoformat(),
                            "due": due.isoformat()})
    follows.sort(key=lambda f: f["due"])
    channels = []
    for key in ("whatsapp", "instagram", "tiktok", "email"):
        n = sum(1 for c in chats if c["channel"] == key)
        sold = [o for o in paid if s.get_customer(o["customer_id"])["channel"] == key]
        channels.append({"key": key, "name": CHANNEL_NAMES[key], "chats": n,
                         "sold": (f"{len(sold)} order{'s' if len(sold) != 1 else ''} · {money(sum(o['total'] for o in sold))}"
                                  if sold else "No sales yet")})
    waiting = sum(o["total"] for o in s.orders(("awaiting_payment",)))
    return {
        "shop": shop.name, "owner": shop.owner,
        "date": now.strftime("%A %d %b").upper(),
        "greeting": "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening",
        "headline": _headline(len(chats), len(paid), len(needs)),
        "sold": money(sum(o["total"] for o in paid)), "waiting": money(waiting),
        "follow_count": sum(1 for f in follows if not f["skipped"]), "needs_count": len(needs),
        "needs": needs, "follows": follows, "channels": channels, "paypal": _mode(desk),
    }


def _headline(chats: int, sold: int, needs: int) -> str:
    s = (f"Since last night the robot answered {chats} chat{'s' if chats != 1 else ''} and PayPal confirmed "
         f"{sold} order{'s' if sold != 1 else ''}.")
    s += " Nothing is waiting on you." if not needs else (" One thing is waiting on you." if needs == 1 else f" {needs} things are waiting on you.")
    return s


def brief_text(desk) -> str:
    b = brief(desk)
    lines = [f"{b['greeting']}, {b['owner']}. {b['headline']}", f"Paid since yesterday: {b['sold']}."]
    for n in b["needs"][:5]:
        lines.append(f"- {n['title']}")
    return "\n".join(lines)


def board(desk) -> dict:
    """One flat row per order for the AG Grid board, plus the latest PayPal events."""
    s = desk.store
    money = desk.shop.money
    cutoff = desk.now() - timedelta(days=14)
    rows = []
    for o in s.orders(tuple(STAGE_LABELS)):
        if o["stage"] == "delivered" and parse(o["delivered_at"] or o["updated"]) < cutoff:
            continue
        c = s.get_customer(o["customer_id"])
        step, yours = next_step(desk, o, c)
        rows.append({"order_id": o["id"], "customer_id": c["id"], "code": desk.code(o["id"]), "name": _who(c),
                     "channel": CHANNEL_NAMES.get(c["channel"], c["channel"]), "what": _what(desk, o),
                     "total": o["total"], "amount": money(o["total"]) if o["total"] else "", "stage": o["stage"],
                     "stage_label": STAGE_LABELS[o["stage"]], "paypal": o["pp_note"] or "No link yet",
                     "says_paid": bool(o["says_paid"]), "next": step, "yours": yours, "updated": o["updated"]})
    paid = paid_since(desk, desk.now() - timedelta(days=7))
    waiting = sum(o["total"] for o in s.orders(("awaiting_payment",)))
    events = [{"id": e["event_id"], "type": e["event_type"], "outcome": e["outcome"], "note": e["note"],
               "when": _when(desk, e["ts"]), "order": desk.code(e["order_id"]) if e["order_id"] else ""}
              for e in s.events(12)]
    return {"shop": desk.shop.name, "rows": rows, "events": events, "paypal": _mode(desk),
            "totals": [{"k": "Paid this week", "v": money(sum(o["total"] for o in paid))},
                       {"k": "Awaiting PayPal", "v": money(waiting)},
                       {"k": "Need your OK", "v": str(len(s.orders(("approval",))))}]}


def chat(desk, customer_id: int) -> dict | None:
    s = desk.store
    c = s.get_customer(customer_id)
    if not c:
        return None
    o = s.open_order(customer_id)
    if not o:
        rows = [r for r in s.orders() if r["customer_id"] == customer_id]
        o = rows[0] if rows else None
    mem = s.memory(c)
    labels = {"area": "Delivers to", "likes": "Likes", "event": "Event", "notes": "Notes"}
    past = [r for r in s.orders(("paid", "delivered")) if r["customer_id"] == customer_id and (not o or r["id"] != o["id"])]
    memory = [{"k": "Orders before", "v": str(len(past)) if past else "New customer"}]
    memory += [{"k": labels[k], "v": v} for k, v in mem.items() if v and k in labels]
    step, yours = next_step(desk, o, c) if o else ("", False)
    return {
        "customer_id": c["id"], "name": _who(c), "channel": CHANNEL_NAMES.get(c["channel"], c["channel"]),
        "owner_mode": bool(c["owner_mode"]), "opted_out": bool(c["opted_out"]),
        "messages": [{"who": m["who"], "text": m["text"], "when": _when(desk, m["ts"])} for m in s.history(customer_id, 60)],
        "memory": memory,
        "order": None if not o else {"id": o["id"], "code": desk.code(o["id"]), "stage": o["stage"], "what": _what(desk, o),
                                     "total": desk.shop.money(o["total"]) if o["total"] else "", "says_paid": bool(o["says_paid"]),
                                     "paid_by": o["paid_by"], "paypal": o["pp_note"]},
        "next": step, "yours": yours,
    }
