"""Sample chats and orders for the made-up demo shop, timed relative to now so the brief looks like a real morning."""

from __future__ import annotations

from datetime import timedelta

from .desk import PRICE_TALK as PRICE
from .shop import load_shop
from .store import iso


def seed(desk) -> None:
    s = desk.store
    s.reset()
    desk.shop = load_shop(desk.settings.shop_dir)
    desk._summary_day = ""
    now = desk.now()
    P = desk.shop.products

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

    c = person("whatsapp", "chioma", "Chioma O.", {"area": "Independence Layout", "likes": "bold Ankara, blue and gold"}, [
        ("customer", "Good evening, is the blue Ankara still available? 6 yards"),
        ("robot", "Yes, we have it. 6 yards of Blue and gold Ankara: ₦36,000. Delivery to Enugu city: ₦2,500. Total: ₦38,500. Shall I hold it for you?"),
        ("customer", "Yes hold it. Send account"),
        ("robot", "Great, I'm holding it for you. Please pay ₦38,500 to Nkem Fabrics, Moniepoint, 0123456789 (demo). Put NF-1 in the transfer description, then send the receipt here."),
        ("customer", "I have sent it, abeg check"),
        ("robot", "Thank you! Nkem is checking the payment now. You'll get a message the moment it's confirmed."),
    ], ago(hours=11))
    order(c, "awaiting_payment", "ANK-BLU", 6, "ENUGU", ago(hours=11), says_paid=1, receipt="I have sent it, abeg check")

    c = person("instagram", "tunde", "Tunde A.", {"likes": "Swiss lace"}, [
        ("customer", "Do you have the Swiss lace in red?"),
        ("robot", "We have it in wine and in coral, both ₦14,000 per yard. Which one do you like?"),
    ], ago(hours=3))
    s.create_order(c, ago(hours=3))

    c = person("tiktok", "blessing", "Blessing E.", {"area": "Nsukka"}, [
        ("customer", "How much is delivery to Nsukka?"),
        ("robot", "Delivery to Nsukka is ₦4,000 and takes one day. Which fabric are you looking at?"),
    ], ago(hours=9))
    s.create_order(c, ago(hours=9))

    c = person("email", "okafor", "Mrs. Okafor", {"notes": "boutique in Abakpa; two bulk orders this year"}, [
        ("customer", "Bulk order for my boutique, 40 yards of the gold George. What is your best price?"),
        ("robot", "Prices are set by Nkem herself, so I've passed this to her. She'll reply you shortly."),
    ], ago(hours=10))
    s.create_order(c, ago(hours=10))
    s.add_handoff(c, "price or bulk request (the robot never changes prices)",
                  "Bulk order for my boutique, 40 yards of the gold George. What is your best price?", ago(hours=10))

    last_in = now + timedelta(hours=2) - timedelta(hours=desk.shop.follow_up_hours * 2)
    c = person("whatsapp", "ifeanyi", "Ifeanyi K.", {"event": "wedding on 21 Nov", "area": "GRA"}, [
        ("customer", "How much for the aso-oke set? I'm in GRA"),
        ("robot", "1 set of Aso-oke full set with gele and ipele: ₦85,000. Delivery to Enugu city: ₦2,500. Total: ₦87,500. Shall I hold it for you?"),
    ], last_in)
    oid = order(c, "quoted", "ASO-SET", 1, "ENUGU", last_in, follow_ups=1)
    s.add_message(c, "robot", "Hi Ifeanyi, still interested in the Aso-oke full set with gele and ipele? I can hold it for you today.",
                  last_in + timedelta(hours=desk.shop.follow_up_hours))

    last_in = now + timedelta(hours=4) - timedelta(hours=desk.shop.follow_up_hours)
    c = person("instagram", "adaeze", "Adaeze N.", {"likes": "wine lace", "area": "New Haven"}, [
        ("customer", "5 yards of the wine lace please, New Haven"),
        ("robot", "5 yards of Swiss lace in wine: ₦70,000. Delivery to Enugu city: ₦2,500. Total: ₦72,500. Shall I hold it for you?"),
    ], last_in)
    order(c, "quoted", "LACE-WINE", 5, "ENUGU", last_in)

    c = person("instagram", "amaka", "Amaka N.", {"area": "Trans-Ekulu", "notes": "prefers afternoon delivery"}, [
        ("customer", "4 yards of the gold George, Trans-Ekulu"),
        ("robot", "4 yards of Gold George wrapper: ₦52,000. Delivery to Enugu city: ₦2,500. Total: ₦54,500. Shall I hold it for you?"),
        ("customer", "Yes"),
        ("robot", "Great, I'm holding it for you. Please pay ₦54,500 to Nkem Fabrics, Moniepoint, 0123456789 (demo). Put NF-7 in the transfer description, then send the receipt here."),
        ("robot", "Payment received, thank you Amaka! Your order NF-7 is confirmed. It goes out same or next day."),
    ], ago(hours=12))
    oid = order(c, "paid", "GEORGE-GLD", 4, "ENUGU", ago(hours=12), paid_by="bank_alert", paid_at=iso(ago(hours=11, minutes=30)))
    s.add_alert(ago(hours=11, minutes=30), msg_id="seed-amaka", amount=54500, sender="AMAKA NNAJI", narration=f"NF-{oid}",
                bank="moniepoint.com", verified=1, order_id=oid, status="confirmed", note="amount and order code; signed by moniepoint.com")

    c = person("whatsapp", "ngozi", "Ngozi U.", {"notes": "picks up at the shop"}, [
        ("customer", "Good morning. 8 yards of the red Ankara, I'll pick up"),
        ("robot", "8 yards of Red Kente-print Ankara: ₦44,000. Pickup at the shop: free. Total: ₦44,000. Shall I hold it for you?"),
        ("customer", "Ok sending now"),
        ("robot", "Payment received, thank you Ngozi! Your order is confirmed. It's ready for pickup."),
    ], ago(hours=2))
    oid = order(c, "paid", "ANK-RED", 8, "PICKUP", ago(hours=2), paid_by="bank_alert", paid_at=iso(ago(hours=1, minutes=40)))
    s.add_alert(ago(hours=1, minutes=40), msg_id="seed-ngozi", amount=44000, sender="NGOZI UDEH", narration="fabric",
                bank="gtbank.com", verified=1, order_id=oid, status="confirmed", note="amount and name; signed by gtbank.com")

    c = person("whatsapp", "kunle", "Kunle B.", {"area": "Uwani"}, [
        ("customer", "Got it, thank you!"),
    ], ago(days=2))
    order(c, "delivered", "LINEN-PLN", 2, "ENUGU", ago(days=3), paid_by="owner", paid_at=iso(ago(days=3)), delivered_at=iso(ago(days=2)))

    c = person("instagram", "kelechi", "Kelechi O.", {"area": "Achara Layout"}, [
        ("customer", "The coral lace I got on Saturday has a tear near the edge"),
        ("robot", "I'm sorry about that. I've passed this to Nkem and she'll reply you herself."),
    ], ago(hours=1))
    order(c, "delivered", "LACE-CORAL", 5, "ENUGU", ago(days=5), paid_by="owner", paid_at=iso(ago(days=5)), delivered_at=iso(ago(days=4)))
    s.add_handoff(c, "complaint: lace arrived torn", "The coral lace I got on Saturday has a tear near the edge", ago(hours=1))

    s.add_alert(ago(hours=5), msg_id="seed-unmatched", amount=20000, sender="EMEKA JOHN", narration="", bank="moniepoint.com",
                verified=1, status="unmatched", note="no order waiting for that amount")

    # Who came from which Meta ad (demo/ads.json). In a real shop this comes from the WhatsApp referral.
    ANKARA, LACE, ASO = "120211000000000101", "120211000000000102", "120211000000000103"

    def from_ad(slug, channel, ad, name, hours):
        cid = s.customer(channel, f"demo-{slug}", now)["id"]
        s.set_ad(cid, ad, name, f"seed-click-{slug}", ago(hours=hours))

    from_ad("chioma", "whatsapp", ANKARA, "Ankara Sunday sale", 11)
    from_ad("ngozi", "whatsapp", ANKARA, "Ankara Sunday sale", 2)
    from_ad("ifeanyi", "whatsapp", ASO, "Aso-oke bridal set", 6)

    c = person("whatsapp", "funmi", "Funmi A.", {"likes": "Ankara"}, [
        ("customer", "Good evening, I saw your Ankara ad. Do you have green?"),
        ("robot", "We have Blue and gold Ankara (₦6,000 per yard) and Red Kente-print Ankara (₦5,500 per yard). Would one of those work?"),
    ], ago(hours=7))
    s.create_order(c, ago(hours=7))
    from_ad("funmi", "whatsapp", ANKARA, "Ankara Sunday sale", 7)

    c = person("whatsapp", "zainab", "Zainab M.", {"event": "traditional wedding, December", "area": "GRA"}, [
        ("customer", "Is the aso-oke set from your ad still available? Full set with gele"),
        ("robot", "1 set of Aso-oke full set with gele and ipele: ₦85,000. Delivery to Enugu city: ₦2,500. Total: ₦87,500. Shall I hold it for you?"),
        ("customer", "Yes please"),
        ("robot", "Payment received, thank you Zainab! Your order is confirmed. It goes out same or next day."),
    ], ago(hours=15))
    oid = order(c, "paid", "ASO-SET", 1, "ENUGU", ago(hours=15), paid_by="bank_alert", paid_at=iso(ago(hours=14)))
    s.add_alert(ago(hours=14), msg_id="seed-zainab", amount=87500, sender="ZAINAB MUSA", narration=f"NF-{oid}",
                bank="moniepoint.com", verified=1, order_id=oid, status="confirmed", note="amount and order code; signed by moniepoint.com")
    from_ad("zainab", "whatsapp", ASO, "Aso-oke bridal set", 15)

    lace_chats = [
        ("chinwe", "Chinwe E.", ["How much is the lace in your ad?", "Last price?"]),
        ("obinna", "Obinna I.", ["Price for the wine lace", "14k per yard is too much, reduce it"]),
        ("uju", "Uju O.", ["I need lace for my sister's wedding", "What's your best price for 5 yards?"]),
        ("ebuka", "Ebuka N.", ["Hello, is this lace original Swiss?", "Ok how much"]),
        ("ada", "Ada C.", ["Coral lace price?", "Can you do 10k per yard?"]),
    ]
    for i, (slug, name, said) in enumerate(lace_chats):
        at = ago(hours=13 - i * 2)
        haggle = PRICE.search(said[1]) is not None
        c = person("whatsapp", slug, name, {"likes": "Swiss lace"}, [
            ("customer", said[0]),
            ("robot", "Our Swiss lace is ₦14,000 per yard, in wine and coral. How many yards do you need?"),
            ("customer", said[1]),
            ("robot", "Prices are set by Nkem herself, so I've passed this to her. She'll reply you shortly." if haggle
             else "It's ₦14,000 per yard. How many yards would you like?"),
        ] + ([("owner", "Sorry dear, the price is fixed. It's real Swiss lace.")] if haggle else []), at)
        s.create_order(c, at)
        from_ad(slug, "whatsapp", LACE, "Lace for owambe", 13 - i * 2)
