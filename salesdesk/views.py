"""What the owner's screens show: the morning brief, the order board and one chat."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .shop import naira
from .store import parse

CHANNEL_NAMES = {"whatsapp": "WhatsApp", "instagram": "Instagram", "tiktok": "TikTok", "email": "Email",
                 "telegram": "Telegram", "web": "Web chat"}
COLUMNS = [("asking", "Asking"), ("quoted", "Price sent"), ("awaiting_payment", "Waiting for money"),
           ("paid", "Paid, to send"), ("delivered", "Delivered")]


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
    if any(a["order_id"] == order["id"] for a in desk.store.alerts(("to_check",))):
        return "Bank alert to check", True
    if stage == "awaiting_payment" and order["says_paid"]:
        return "Says paid: check your bank", True
    if stage == "paid":
        return "Send it, then mark delivered", True
    if stage == "delivered":
        return "Done", False
    due = desk.follow_up_due(order)
    if due:
        return f"Robot follows up {_when(desk, due.isoformat())}", False
    if stage in ("quoted", "awaiting_payment"):
        return "Robot is waiting for them", False
    return "Robot is answering", False


def paid_since(desk, since: datetime) -> list:
    return [o for o in desk.store.orders(("paid", "delivered")) if o["paid_at"] and parse(o["paid_at"]) >= since]


def brief(desk) -> dict:
    s, shop = desk.store, desk.shop
    now = desk.now()
    paid = paid_since(desk, _since_yesterday(desk))
    chats = s.inbound_since(_overnight(desk))
    needs = []
    for o in s.orders(("awaiting_payment",)):
        if o["says_paid"]:
            c = s.get_customer(o["customer_id"])
            needs.append({"kind": "payment", "order_id": o["id"], "customer_id": c["id"],
                          "channel": CHANNEL_NAMES.get(c["channel"], c["channel"]), "when": _when(desk, o["updated"]),
                          "title": f"{_who(c)} says they paid {naira(o['total'])}",
                          "detail": f"{desk.code(o['id'])}: {_what(desk, o)}. Check your bank app, or wait for the bank alert."})
    for a in s.alerts(("to_check",)):
        o = s.get_order(a["order_id"]) if a["order_id"] else None
        c = s.get_customer(o["customer_id"]) if o else None
        needs.append({"kind": "alert", "alert_id": a["id"], "order_id": a["order_id"], "customer_id": c["id"] if c else None,
                      "channel": "Bank alert", "when": _when(desk, a["ts"]),
                      "title": f"{naira(a['amount'])} from {a['sender'] or 'unknown sender'}" + (f" looks like {_who(c)}'s payment" if c else ""),
                      "detail": f"Not confirmed by itself: {a['note']}."})
    for h in s.open_handoffs():
        c = s.get_customer(h["customer_id"])
        needs.append({"kind": "handoff", "customer_id": c["id"], "channel": CHANNEL_NAMES.get(c["channel"], c["channel"]),
                      "when": _when(desk, h["created"]), "title": f"{_who(c)}: {h['reason']}",
                      "detail": f"They said: \"{h['question'][:220]}\""})
    unmatched = [{"alert_id": a["id"], "amount": naira(a["amount"]), "sender": a["sender"] or "unknown sender",
                  "when": _when(desk, a["ts"]), "verified": bool(a["verified"])} for a in s.alerts(("unmatched",))[:5]]
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
                         "sold": f"{len(sold)} order{'s' if len(sold) != 1 else ''} · {naira(sum(o['total'] for o in sold))}" if sold else "No sales yet"})
    waiting = sum(o["total"] for o in s.orders(("awaiting_payment",)) if o["says_paid"])
    by_alert = sum(1 for o in paid if o["paid_by"] == "bank_alert")
    return {
        "shop": shop.name, "owner": shop.owner,
        "date": now.strftime("%A %d %b").upper(),
        "greeting": "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening",
        "headline": _headline(len(chats), len(paid), by_alert, len(needs)),
        "sold": naira(sum(o["total"] for o in paid)), "waiting": naira(waiting),
        "follow_count": sum(1 for f in follows if not f["skipped"]), "needs_count": len(needs),
        "needs": needs, "follows": follows, "channels": channels, "unmatched": unmatched,
        "bank_reading": bool(desk.settings.imap_host),
    }


PLATFORMS = {"facebook": "Facebook", "instagram": "Instagram", "messenger": "Messenger", "audience_network": "Audience Network"}
VERDICTS = {"best": "Best ad", "working": "Working", "losing": "Losing money", "too_early": "Too early", "free": "No spend"}


def ads_card(rep: dict, notes: dict | None, live: bool, error: str = "") -> dict:
    """The "Your ads yesterday" card: spend against confirmed sales, per ad, with Muse's note when it's ready."""
    top = max([r["per_naira"] or 0 for r in rep["rows"]] + [1])
    rows = []
    for r in rep["rows"]:
        where = " → ".join(x for x in (PLATFORMS.get(r["platform"], r["platform"].title()),
                                       CHANNEL_NAMES.get(r["lands"], "")) if x) or "Meta ad"
        note = (notes or {}).get(r["id"]) or {}
        rows.append({"id": r["id"], "name": r["name"], "where": where, "platform": r["platform"],
                     "spend": naira(r["spend"]), "chats": r["chats"], "paid": r["paid"], "back": naira(r["back"]),
                     "per": f"₦{r['per_naira']:g} back" if r["per_naira"] else VERDICTS[r["verdict"]],
                     "per_big": f"₦{r['per_naira']:g}" if r["per_naira"] else "₦0",
                     "bar": round(100 * (r["per_naira"] or 0) / top), "verdict": r["verdict"],
                     "verdict_label": VERDICTS[r["verdict"]], "haggled": r["haggled"],
                     "note": note.get("note", ""), "by_muse": note.get("by", "rules") not in ("", "rules"),
                     "picture": r["verdict"] in ("losing", "too_early") and bool(r["spend"])})
    return {"live": live, "error": error, "notes_ready": notes is not None, "rows": rows,
            "spend": naira(rep["spend"]), "back": naira(rep["back"]), "chats": rep["chats"], "paid": rep["paid"]}


def _headline(chats: int, sold: int, by_alert: int, needs: int) -> str:
    s = (f"Since last night the robot answered {chats} chat{'s' if chats != 1 else ''} and closed "
         f"{sold} order{'s' if sold != 1 else ''}.")
    if by_alert:
        s += f" {by_alert} payment{'s were' if by_alert != 1 else ' was'} confirmed from your bank alerts."
    s += " Nothing is waiting on you." if not needs else (" One thing is waiting on you." if needs == 1 else f" {needs} things are waiting on you.")
    return s


def brief_text(desk) -> str:
    b = brief(desk)
    lines = [f"{b['greeting']}, {b['owner']}. {b['headline']}", f"Sold since yesterday: {b['sold']}."]
    for n in b["needs"][:5]:
        lines.append(f"- {n['title']}")
    return "\n".join(lines)


def board(desk) -> dict:
    s = desk.store
    cutoff = desk.now() - timedelta(days=14)
    cols = {k: [] for k, _ in COLUMNS}
    for o in s.orders(tuple(cols)):
        if o["stage"] == "delivered" and parse(o["delivered_at"] or o["updated"]) < cutoff:
            continue
        c = s.get_customer(o["customer_id"])
        step, yours = next_step(desk, o, c)
        cols[o["stage"]].append({"order_id": o["id"], "customer_id": c["id"], "name": _who(c),
                                 "channel": CHANNEL_NAMES.get(c["channel"], c["channel"]), "channel_key": c["channel"],
                                 "what": _what(desk, o), "amount": naira(o["total"]) if o["total"] else "",
                                 "next": step, "yours": yours, "code": desk.code(o["id"])})
    paid = paid_since(desk, desk.now() - timedelta(days=7))
    waiting = sum(o["total"] for o in s.orders(("awaiting_payment",)))
    return {"shop": desk.shop.name,
            "columns": [{"key": k, "title": t, "cards": cols[k]} for k, t in COLUMNS],
            "totals": [{"k": "Paid this week", "v": naira(sum(o["total"] for o in paid))},
                       {"k": "Waiting for money", "v": naira(waiting)},
                       {"k": "Open chats", "v": str(len(cols["asking"]) + len(cols["quoted"]))}]}


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
                                     "total": naira(o["total"]) if o["total"] else "", "says_paid": bool(o["says_paid"]),
                                     "paid_by": o["paid_by"]},
        "next": step, "yours": yours,
    }
