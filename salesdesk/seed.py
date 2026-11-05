"""Sample chats and orders for the made-up demo shop, timed relative to now so the brief looks like a real morning.

Nothing here is waiting on a live PayPal link: orders that need one start at "approval", so the first thing
a viewer does is approve one and watch the real flow make the link."""

from __future__ import annotations

from datetime import timedelta

from .desk import PRICE_TALK as PRICE  # noqa: F401  (kept so old imports still work)
from .shop import load_shop
from .store import iso


def seed(desk) -> None:
    s = desk.store
    s.reset()
    desk.shop = load_shop(desk.settings.shop_dir)
    desk._summary_day = ""
    now = desk.now()
    P = desk.shop.products
    money = desk.shop.money

    def ago(**kw):
        return now - timedelta(**kw)

    def person(channel, slug, name, memory, chat, at):
        c = s.customer(channel, f"demo-{slug}", at)
        s.update_customer(c["id"], name=name, memory=memory)
        t = at
        for who, text in chat:
            s.add_message(c["id"], who, text, t)
            t += timedelta(minutes=2)
        return c["id"]

    def order(cid, stage, sku, qty, zone, at, **extra):
        o = s.create_order(cid, at)
        p, z = P[sku], desk.shop.zones[zone]
        sub = p.price * qty
        s.update_order(o["id"], at, stage=stage, zone=zone, subtotal=sub, delivery_fee=z.fee, total=sub + z.fee,
                       items=[{"sku": sku, "name": p.name, "unit": p.unit, "qty": qty, "price": p.price}], **extra)
        return o["id"]

    def paid(cid, sku, qty, zone, at, n):
        oid = order(cid, "paid", sku, qty, zone, at, paid_by="paypal", paid_at=iso(at + timedelta(minutes=20)),
                    pp_order_id=f"SEED-PP-{n}", pp_capture_id=f"SEED-CAP-{n}", pp_note=f"Paid: PayPal capture SEED-CAP-{n}")
        s.add_event(at + timedelta(minutes=20), f"WH-SEED-{n}", "PAYMENT.CAPTURE.COMPLETED", oid, "paid", f"capture SEED-CAP-{n}")
        return oid

    c = person("whatsapp", "chioma", "Chioma O.", {"area": "Peckham", "likes": "bold Ankara, blue and gold"}, [
        ("customer", "Good evening, is the blue Ankara still available? 6 yards"),
        ("robot", f"Yes, we have it. 6 yards of Blue and gold Ankara: {money(72)}. Delivery to Greater London: {money(4)}. "
                  f"Total: {money(76)}. Shall I hold it for you?"),
        ("customer", "Yes hold it"),
        ("robot", f"Lovely, I'm holding it for you. Folake approves each order first, then I'll send your secure PayPal link "
                  f"for {money(76)} right here. It only takes a moment."),
    ], ago(hours=3))
    order(c, "approval", "ANK-BLU", 6, "LONDON", ago(hours=3), pp_note="Waiting for your OK")

    c = person("instagram", "tunde", "Tunde A.", {"likes": "Swiss lace"}, [
        ("customer", "Do you have the Swiss lace in red?"),
        ("robot", f"We have it in wine and in coral, both {money(28)} per yard. Which one do you like?"),
    ], ago(hours=3))
    s.create_order(c, ago(hours=3))

    c = person("email", "okafor", "Mrs. Okafor", {"notes": "boutique in Croydon; two bulk orders this year"}, [
        ("customer", "Bulk order for my boutique, 40 yards of the gold George. What is your best price?"),
        ("robot", "Prices are set by Folake herself, so I've passed this to her. She'll reply you shortly."),
    ], ago(hours=10))
    s.create_order(c, ago(hours=10))
    s.add_handoff(c, "price or bulk request (the robot never changes prices)",
                  "Bulk order for my boutique, 40 yards of the gold George. What is your best price?", ago(hours=10))

    last_in = now + timedelta(hours=2) - timedelta(hours=desk.shop.follow_up_hours * 2)
    c = person("whatsapp", "ifeanyi", "Ifeanyi K.", {"event": "wedding on 21 Nov", "area": "Hackney"}, [
        ("customer", "How much for the aso-oke set? I'm in Hackney"),
        ("robot", f"1 set of Aso-oke full set with gele and ipele: {money(165)}. Delivery to Greater London: {money(4)}. "
                  f"Total: {money(169)}. Shall I hold it for you?"),
    ], last_in)
    order(c, "quoted", "ASO-SET", 1, "LONDON", last_in, follow_ups=1)
    s.add_message(c, "robot", "Hi Ifeanyi, still interested in the Aso-oke full set with gele and ipele? I can hold it for you today.",
                  last_in + timedelta(hours=desk.shop.follow_up_hours))

    last_in = now + timedelta(hours=4) - timedelta(hours=desk.shop.follow_up_hours)
    c = person("instagram", "adaeze", "Adaeze N.", {"likes": "wine lace", "area": "Croydon"}, [
        ("customer", "5 yards of the wine lace please, Croydon"),
        ("robot", f"5 yards of Swiss lace in wine: {money(140)}. Delivery to Greater London: {money(4)}. "
                  f"Total: {money(144)}. Shall I hold it for you?"),
    ], last_in)
    order(c, "quoted", "LACE-WINE", 5, "LONDON", last_in)

    c = person("instagram", "amaka", "Amaka N.", {"area": "Manchester", "notes": "prefers afternoon delivery"}, [
        ("customer", "4 yards of the gold George, Manchester"),
        ("robot", f"4 yards of Gold George wrapper: {money(104)}. Delivery to the rest of the UK: {money(6)}. "
                  f"Total: {money(110)}. Shall I hold it for you?"),
        ("customer", "Yes"),
        ("robot", f"Here is your secure PayPal link for {money(110)} (ZC-6): (seed demo) You can pay with PayPal or a card."),
        ("robot", "Payment received through PayPal, thank you Amaka! Your order ZC-6 is confirmed. It goes out two to three working days."),
    ], ago(hours=12))
    paid(c, "GEORGE-GLD", 4, "UK", ago(hours=12), 1)

    c = person("whatsapp", "ngozi", "Ngozi U.", {"notes": "collects from the shop"}, [
        ("customer", "Good morning. 8 yards of the red Ankara, I'll collect"),
        ("robot", f"8 yards of Red Kente-print Ankara: {money(88)}. Collection from the shop: free. Total: {money(88)}. Shall I hold it for you?"),
        ("robot", "Payment received through PayPal, thank you Ngozi! Your order ZC-7 is confirmed. It's ready for collection."),
    ], ago(hours=2))
    paid(c, "ANK-RED", 8, "COLLECT", ago(hours=2), 2)

    c = person("whatsapp", "kunle", "Kunle B.", {"area": "Lewisham"}, [("customer", "Got it, thank you!")], ago(days=2))
    order(c, "delivered", "LINEN-PLN", 2, "LONDON", ago(days=3), paid_by="paypal", paid_at=iso(ago(days=3)),
          delivered_at=iso(ago(days=2)), pp_order_id="SEED-PP-3", pp_capture_id="SEED-CAP-3", pp_note="Paid: PayPal capture SEED-CAP-3")

    c = person("instagram", "kelechi", "Kelechi O.", {"area": "Catford"}, [
        ("customer", "The coral lace I got on Saturday has a tear near the edge"),
        ("robot", "I'm sorry about that. I've passed this to Folake and she'll reply you herself."),
    ], ago(hours=1))
    order(c, "delivered", "LACE-CORAL", 5, "LONDON", ago(days=5), paid_by="paypal", paid_at=iso(ago(days=5)),
          delivered_at=iso(ago(days=4)), pp_order_id="SEED-PP-4", pp_capture_id="SEED-CAP-4", pp_note="Paid: PayPal capture SEED-CAP-4")
    s.add_handoff(c, "complaint: lace arrived torn", "The coral lace I got on Saturday has a tear near the edge", ago(hours=1))
