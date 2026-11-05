import asyncio

from aiohttp.test_utils import TestClient, TestServer
from test_payments import eml

from salesdesk.deliver import Dispatcher
from salesdesk.seed import seed
from salesdesk.web import Web


def run(coro):
    return asyncio.run(coro)


def _client(desk, **settings):
    for name, value in settings.items():
        setattr(desk.settings, name, value)
    return TestClient(TestServer(Web(desk.settings, desk, Dispatcher(desk.store)).app()))


def test_screens_and_owner_actions(make_desk):
    async def go():
        d = make_desk()
        seed(d)
        async with _client(d) as c:
            for path in ("/", "/board", "/phone", "/app.css"):
                assert (await c.get(path)).status == 200
            brief = await (await c.get("/api/brief")).json()
            pay = next(n for n in brief["needs"] if n["kind"] == "payment")
            assert (await c.post(f"/api/order/{pay['order_id']}/confirm", json={})).status == 200
            assert d.store.get_order(pay["order_id"])["stage"] == "paid"
            chat = await (await c.get(f"/api/chat/{pay['customer_id']}")).json()
            assert chat["messages"][-1]["text"].startswith("Payment received")
            r = await c.post(f"/api/customer/{pay['customer_id']}/reply", json={"text": "Thank you dear!"})
            assert (await r.json())["sent"] and d.store.get_customer(pay["customer_id"])["owner_mode"] == 1
    run(go())


def test_demo_phone_and_test_alert(make_desk):
    async def go():
        d = make_desk()
        async with _client(d) as c:
            for t in ("6 yards of the blue ankara please", "GRA", "yes"):
                r = await (await c.post("/api/demo/message", json={"channel": "instagram", "address": "x1", "name": "Ada", "text": t})).json()
            assert "NF-1" in r["replies"][0]
            assert (await c.post("/api/demo/bank-alert", json={"amount": "38,500", "narration": "NF-1", "verified": True})).status == 200
            inbox = await (await c.get("/api/demo/inbox?channel=instagram&address=x1")).json()
            assert inbox["replies"] and inbox["replies"][0].startswith("Payment received")
    run(go())


def test_inbound_bank_alert_needs_the_token(make_desk):
    async def go():
        d = make_desk()
        async with _client(d, inbound_token="t0k", alert_domains=("moniepoint.com",)) as c:
            assert (await c.post("/inbound/bank-alert", data=eml())).status == 403
            r = await c.post("/inbound/bank-alert?token=t0k", data=eml())
            assert (await r.json()) == {"credit_alert": True, "verified": True}
            assert d.store.alerts(("unmatched",))  # no order waiting in an empty shop
    run(go())


def test_demo_key_locks_screens_and_demo_tools_can_be_off(make_desk):
    async def go():
        d = make_desk()
        async with _client(d, demo_key="nkem-7") as c:
            assert (await c.get("/")).status == 404 and (await c.get("/api/brief")).status == 404
            assert (await c.get("/?k=nkem-7")).status == 200 and (await c.get("/health")).status == 200
        async with _client(make_desk(), demo_tools=False) as c:
            assert (await c.get("/phone")).status == 404
            assert (await c.post("/api/demo/reset", json={})).status in (404, 405)
    run(go())
