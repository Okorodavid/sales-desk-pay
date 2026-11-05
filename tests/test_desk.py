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


def test_full_sale_quotes_from_the_price_list_then_waits_for_money(make_desk):
    d = make_desk()
    assert "fabric" in texts(say(d, "Hello"))[0]
    first = texts(say(d, "6 yards of the blue ankara please"))[0]
    assert "₦36,000" in first and "Where should we deliver" in first
    quote = texts(say(d, "Independence Layout"))[0]
    assert "Total: ₦38,500" in quote  # 6 x 6,000 + 2,500 Enugu delivery
    pay = texts(say(d, "yes hold it"))[0]
    assert "NF-1" in pay and "₦38,500" in pay
    o = d.store.get_order(1)
    assert o["stage"] == "awaiting_payment" and o["total"] == 38500
    cust = d.store.get_customer(o["customer_id"])
    assert d.store.memory(cust)["area"] == "Independence Layout"


def test_model_price_is_never_used(make_desk):
    brain = ScriptedBrain(turn(reply="That will be ₦1,000 total!", action="quote", items=[{"sku": "ANK-BLU", "qty": 6}], zone="ENUGU"))
    d = make_desk(brain)
    reply = texts(say(d, "6 yards blue ankara to GRA"))[0]
    assert "₦1,000" not in reply and "Total: ₦38,500" in reply


def test_a_reply_with_an_unchecked_amount_goes_to_the_owner(make_desk):
    d = make_desk(ScriptedBrain(turn(reply="Delivery to Lagos is ₦3,000.")))
    out = say(d, "How much to Lagos?")
    assert "₦3,000" not in texts(out)[0]
    assert out.notes and "couldn't check" in out.notes[0].text
    assert len(d.store.open_handoffs()) == 1


def test_bargaining_goes_to_the_owner_without_a_model_call(make_desk):
    d = make_desk(ScriptedBrain())  # no turns: a model call would fail the test
    out = say(d, "Abeg what's your last price?")
    assert "Nkem" in texts(out)[0] and d.store.open_handoffs()


def test_bulk_orders_go_to_the_owner(make_desk):
    d = make_desk()
    out = say(d, "40 yards of the gold george wrapper, GRA")
    assert "40 yards" in out.notes[0].text and d.store.get_order(1)["total"] == 0


def test_not_enough_stock_is_handed_over(make_desk):
    d = make_desk()
    out = say(d, "3 sets of the aso-oke set please, GRA")
    assert "only 2" in texts(out)[0] and d.store.open_handoffs()


def test_says_paid_waits_for_the_owner_and_never_claims_the_money_arrived(make_desk):
    d = make_desk()
    for t in ("6 yards of the blue ankara please", "GRA", "yes"):
        say(d, t)
    out = say(d, "I have paid")
    assert "checking" in texts(out)[0] and "received" not in texts(out)[0].lower()
    assert d.store.get_order(1)["says_paid"] == 1
    done = d.confirm_payment(1)
    assert "Payment received" in texts(done)[0]
    assert d.store.get_order(1)["stage"] == "paid" and d.shop.products["ANK-BLU"].stock == 54


def test_payment_not_seen_tells_the_customer(make_desk):
    d = make_desk()
    for t in ("6 yards of the blue ankara please", "GRA", "yes", "sent it"):
        say(d, t)
    out = d.payment_not_seen(1)
    assert "can't see the transfer yet" in texts(out)[0] and d.store.get_order(1)["says_paid"] == 0


def test_owner_takeover_silences_the_robot(make_desk):
    d = make_desk()
    say(d, "Hello")
    cust = d.store.customer("whatsapp", "demo-c1", d.now())
    d.owner_reply(cust["id"], "Hi Chioma, Nkem here!")
    out = say(d, "Hi Nkem, how far?")
    assert not out.replies and "how far" in out.notes[0].text
    d.set_takeover(cust["id"], False)
    assert say(d, "Hello again").replies


def test_stop_opts_out(make_desk):
    d = make_desk()
    assert "won't get more messages" in texts(say(d, "STOP"))[0]
    assert not say(d, "Hello").replies


def test_follow_ups_go_out_after_a_quiet_day_at_most_twice(make_desk, clock):
    d = make_desk()
    for t in ("6 yards of the blue ankara please", "GRA"):
        say(d, t)
    assert not d.tick().replies or all(r.kind != "follow_up" for r in d.tick().replies)
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
    assert b["sold"] == "₦186,000"  # Amaka 54,500 + Ngozi 44,000 + Zainab 87,500, all matched from bank alerts
    assert b["waiting"] == "₦38,500"
    kinds = sorted(n["kind"] for n in b["needs"])
    assert kinds == ["handoff", "handoff", "payment"]
    assert "confirmed from your bank alerts" in b["headline"]
    assert b["unmatched"][0]["amount"] == "₦20,000"
    cols = {c["key"]: len(c["cards"]) for c in views.board(d)["columns"]}
    assert cols == {"asking": 9, "quoted": 2, "awaiting_payment": 1, "paid": 3, "delivered": 2}
    chat = views.chat(d, 1)
    assert chat["name"] == "Chioma O." and any(m["k"] == "Delivers to" for m in chat["memory"])
