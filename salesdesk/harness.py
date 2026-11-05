"""A whole shop in one process, for the tests and the forged-receipt evaluation: the desk, the web app, and the
built-in mock PayPal, talking to each other over real HTTP on a throwaway port. No keys, no internet."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from .brain import RulesBrain
from .config import Settings
from .deliver import Dispatcher
from .desk import Desk
from .mockpaypal import MockPayPal
from .paypal import MOCK_WEBHOOK_ID, PayPalClient
from .shop import load_shop
from .store import Store
from .web import Web

ROOT = Path(__file__).resolve().parents[1]


class Stack:
    def __init__(self, brain=None, senses=None, **settings):
        self.settings = Settings(provider="rules", shop_dir=ROOT / "demo", **settings)
        self.store = Store(":memory:")
        self.mock = MockPayPal(delay=0)
        self.desk = Desk(self.settings, self.store, load_shop(self.settings.shop_dir), brain or RulesBrain(), senses=senses)
        self.dispatcher = Dispatcher(self.store)
        self.web = Web(self.settings, self.desk, self.dispatcher, self.mock)
        self.client = TestClient(TestServer(self.web.app()))
        self.n = 0

    async def __aenter__(self) -> "Stack":
        await self.client.start_server()
        base = str(self.client.make_url("/mock-paypal")).rstrip("/")
        self.settings.public_url = str(self.client.make_url("")).rstrip("/")
        self.desk.paypal = PayPalClient(base, "mock-client", "mock-secret", MOCK_WEBHOOK_ID)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.desk.paypal.aclose()
        await self.client.close()

    # Shortcuts ---------------------------------------------------------------------------------------
    async def say(self, who: str, text: str, channel: str = "whatsapp") -> list[str]:
        out = await self.desk.handle(channel, f"demo-{who}", text, who.title())
        await self.dispatcher.deliver(out)
        return [r.text for r in out.replies]

    async def quote_and_accept(self, who: str, text: str = "6 yards of the blue ankara please", zone: str = "Peckham"):
        """A customer asks, gets a quote and says yes. The order now waits for the owner's approval."""
        await self.say(who, text)
        await self.say(who, zone)
        await self.say(who, "yes")
        cust = self.store.customer("whatsapp", f"demo-{who}", self.desk.now())
        return self.store.open_order(cust["id"])

    async def awaiting_order(self, who: str | None = None, **kw):
        """An order the owner has approved: it has a PayPal link and waits for PayPal to say the money arrived."""
        self.n += 1
        order = await self.quote_and_accept(who or f"buyer{self.n}", **kw)
        await self.dispatcher.deliver(await self.desk.approve_payment(order["id"]))
        return self.store.get_order(order["id"])

    async def buyer_pays(self, order) -> None:
        """What the buyer does on PayPal: open the link, press pay, come back. PayPal then posts its webhook."""
        token = order["pp_order_id"]
        r = await self.client.post(f"/mock-paypal/pay/{token}", allow_redirects=False)
        assert r.status == 303, r.status
        r = await self.client.get(f"/paypal/return?token={token}")
        assert r.status == 200, r.status
        while self.mock.tasks:
            await asyncio.gather(*list(self.mock.tasks))

    async def post_webhook(self, headers: dict, body: bytes):
        return await self.client.post("/paypal/webhook", data=body, headers=headers)

    def stage(self, order) -> str:
        return self.store.get_order(order["id"])["stage"]
