import asyncio

from aiohttp.test_utils import TestClient, TestServer

from salesdesk.deliver import Dispatcher
from salesdesk.harness import Stack
from salesdesk.seed import seed
from salesdesk.web import Web


def run(coro):
    return asyncio.run(coro)


def _client(desk, **settings):
    for name, value in settings.items():
        setattr(desk.settings, name, value)
    return TestClient(TestServer(Web(desk.settings, desk, Dispatcher(desk.store)).app()))


def test_screens_and_the_owner_approve_button(make_desk):
    async def go():
        async with Stack() as st:
            seed(st.desk)
            c = st.client
            for path in ("/", "/board", "/phone", "/app.css"):
                assert (await c.get(path)).status == 200
            brief = await (await c.get("/api/brief")).json()
            need = next(n for n in brief["needs"] if n["kind"] == "approval")
            assert (await c.post(f"/api/order/{need['order_id']}/approve", json={})).status == 200
            order = st.store.get_order(need["order_id"])
            assert order["stage"] == "awaiting_payment" and order["pp_link"]
            chat = await (await c.get(f"/api/chat/{need['customer_id']}")).json()
            assert "secure PayPal link" in chat["messages"][-1]["text"] and chat["order"]["paypal"].startswith("Link sent")
            board = await (await c.get("/api/board")).json()
            row = next(r for r in board["rows"] if r["order_id"] == need["order_id"])
            assert row["stage"] == "awaiting_payment" and row["stage_label"] == "Awaiting PayPal"
            r = await c.post(f"/api/customer/{need['customer_id']}/reply", json={"text": "Thank you dear!"})
            assert (await r.json())["sent"] and st.store.get_customer(need["customer_id"])["owner_mode"] == 1
    run(go())


def test_demo_phone_end_to_end_with_a_forged_receipt_first(make_desk):
    async def go():
        async with Stack() as st:
            c = st.client
            for t in ("6 yards of the blue ankara please", "Peckham", "yes"):
                r = await (await c.post("/api/demo/message", json={"channel": "instagram", "address": "x1", "name": "Ada", "text": t})).json()
            assert "approves each order first" in r["replies"][0]
            brief = await (await c.get("/api/brief")).json()
            oid = next(n for n in brief["needs"] if n["kind"] == "approval")["order_id"]
            await c.post(f"/api/order/{oid}/approve", json={})
            order = st.store.get_order(oid)
            # a forged PayPal message is refused at the door
            r = await (await c.post("/api/demo/forge", json={"order_id": oid, "kind": "wrong_key"})).json()
            assert r["refused"] and r["status"] == 401 and r["stage"] == "awaiting_payment"
            # then the real thing
            await st.buyer_pays(order)
            assert st.stage(order) == "paid"
            inbox = await (await c.get("/api/demo/inbox?channel=instagram&address=x1")).json()
            assert any(m.startswith("Payment received through PayPal") for m in inbox["replies"])
    run(go())


def test_forge_endpoint_needs_a_paypal_order(make_desk):
    async def go():
        async with Stack() as st:
            r = await st.client.post("/api/demo/forge", json={"order_id": 99, "kind": "unsigned"})
            assert r.status == 400
    run(go())


def test_buyer_pages_only_work_for_orders_we_made(make_desk):
    async def go():
        async with Stack() as st:
            assert (await st.client.get("/paypal/return?token=NOPE")).status == 404
            assert (await st.client.get("/paypal/cancel")).status == 200
    run(go())


def test_demo_key_locks_screens_and_demo_tools_can_be_off(make_desk):
    async def go():
        d = make_desk()
        async with _client(d, demo_key="zuri-7") as c:
            assert (await c.get("/")).status == 404 and (await c.get("/api/brief")).status == 404
            assert (await c.get("/?k=zuri-7")).status == 200 and (await c.get("/health")).status == 200
            assert (await c.post("/paypal/webhook", data=b"{}")).status == 503  # no PayPal client in this desk: refuses
        async with _client(make_desk(), demo_tools=False) as c:
            assert (await c.get("/phone")).status == 404
            assert (await c.post("/api/demo/reset", json={})).status in (404, 405)
    run(go())
