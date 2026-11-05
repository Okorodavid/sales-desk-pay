import asyncio

from conftest import ScriptedBrain

from salesdesk import views
from salesdesk.seed import seed


def run(coro):
    return asyncio.run(coro)


def say(desk, text, address="demo-c1", channel="whatsapp", name="Chioma"):
    return run(desk.handle(channel, address, text, name))


def texts(outcome):
    return [r.text for r in outcome.replies]


def turn(**kw):
    base = {"reply": "", "intent": "other", "memory": {}, "action": "none", "items": [], "zone": "",
            "handoff_reason": "", "sources": [], "confidence": "high"}
    base.update(kw)
    return base


def test_full_sale_quotes_from_the_price_list_then_waits_for_the_owner(make_desk):
    d = make_desk()
    assert "fabric" in texts(say(d, "Hello"))[0]
    first = texts(say(d, "6 yards of the blue ankara please"))[0]
    assert "£72" in first and "Where should we deliver" in first
    quote = texts(say(d, "Peckham"))[0]
    assert "Total: £76" in quote  # 6 x £12 + £4 Greater London delivery
    asked = say(d, "yes hold it")
    assert "approves each order first" in texts(asked)[0] and "http" not in texts(asked)[0]
    assert "Approve it" in asked.notes[0].text
    o = d.store.get_order(1)
    assert o["stage"] == "approval" and o["total"] == 76 and not o["pp_link"]
    assert d.store.memory(d.store.get_customer(o["customer_id"]))["area"] == "Peckham"


def test_model_price_is_never_used(make_desk):
    brain = ScriptedBrain(turn(reply="That will be £1 total!", action="quote", items=[{"sku": "ANK-BLU", "qty": 6}], zone="LONDON"))
    d = make_desk(brain)
    reply = texts(say(d, "6 yards blue ankara to Hackney"))[0]
    assert "£1 total" not in reply and "Total: £76" in reply


def test_a_reply_with_an_unchecked_amount_goes_to_the_owner(make_desk):
    d = make_desk(ScriptedBrain(turn(reply="Delivery to Lagos is £3.50.")))
    out = say(d, "How much to Lagos?")
    assert "£3.50" not in texts(out)[0]
    assert out.notes and "couldn't check" in out.notes[0].text
    assert len(d.store.open_handoffs()) == 1


def test_bargaining_goes_to_the_owner_without_a_model_call(make_desk):
    d = make_desk(ScriptedBrain())  # no turns: a model call would fail the test
    out = say(d, "Abeg what's your last price?")
    assert "Folake" in texts(out)[0] and d.store.open_handoffs()


def test_bulk_orders_go_to_the_owner(make_desk):
    d = make_desk()
    out = say(d, "40 yards of the gold george wrapper, Hackney")
    assert "40 yards" in out.notes[0].text and d.store.get_order(1)["total"] == 0


def test_not_enough_stock_is_handed_over(make_desk):
    d = make_desk()
    out = say(d, "3 sets of the aso-oke set please, Hackney")
    assert "only 2" in texts(out)[0] and d.store.open_handoffs()


def test_says_paid_is_a_claim_not_a_payment(make_desk):
    d = make_desk()
    for t in ("6 yards of the blue ankara please", "Peckham", "yes"):
        say(d, t)
    out = say(d, "I have paid")
    reply = texts(out)[0].lower()
    assert "only when paypal says" in reply and "payment received" not in reply
    o = d.store.get_order(1)
    assert o["says_paid"] == 1 and o["stage"] == "approval" and o["paid_by"] == ""
    assert d.shop.products["ANK-BLU"].stock == 60  # stock moves only when PayPal confirms


def test_owner_takeover_silences_the_robot(make_desk):
    d = make_desk()
    say(d, "Hello")
    cust = d.store.customer("whatsapp", "demo-c1", d.now())
    d.owner_reply(cust["id"], "Hi Chioma, Folake here!")
    out = say(d, "Hi Folake, how far?")
    assert not out.replies and "how far" in out.notes[0].text
    d.set_takeover(cust["id"], False)
    assert say(d, "Hello again").replies


def test_stop_opts_out(make_desk):
    d = make_desk()
    assert "won't get more messages" in texts(say(d, "STOP"))[0]
    assert not say(d, "Hello").replies


def test_follow_ups_go_out_after_a_quiet_day_at_most_twice(make_desk, clock):
    d = make_desk()
    for t in ("6 yards of the blue ankara please", "Peckham"):
        say(d, t)
    assert all(r.kind != "follow_up" for r in d.tick().replies)
    clock.advance(hours=21)
    first = [r for r in d.tick().replies if r.kind == "follow_up"]
    assert len(first) == 1 and "still interested" in first[0].text
    assert not [r for r in d.tick().replies if r.kind == "follow_up"]
    clock.advance(hours=20)
    d.skip_follow_up(1)
    assert not [r for r in d.tick().replies if r.kind == "follow_up"]  # skipped today
    clock.advance(hours=24)
    assert [r for r in d.tick().replies if r.kind == "follow_up"]
    clock.advance(hours=48)
    assert not [r for r in d.tick().replies if r.kind == "follow_up"]  # max 2


def test_brief_and_board_from_the_sample_morning(make_desk):
    d = make_desk()
    seed(d)
    b = views.brief(d)
    assert b["sold"] == "£198"  # Amaka £110 + Ngozi £88, both confirmed by PayPal events
    assert b["waiting"] == "£0"
    assert sorted(n["kind"] for n in b["needs"]) == ["approval", "handoff", "handoff"]
    assert "PayPal confirmed 2 orders" in b["headline"]
    board = views.board(d)
    counts = {}
    for row in board["rows"]:
        counts[row["stage"]] = counts.get(row["stage"], 0) + 1
    assert counts == {"asking": 2, "quoted": 2, "approval": 1, "paid": 2, "delivered": 2}
    assert [e["outcome"] for e in board["events"]] == ["paid", "paid"]
    chat = views.chat(d, 1)
    assert chat["name"] == "Chioma O." and any(m["k"] == "Delivers to" for m in chat["memory"])
