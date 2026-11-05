"""A stand-in for PayPal, mounted at /mock-paypal, so the demo and the tests run with no keys and no internet.

It behaves like the real thing in the ways that matter here: it logs in with a client id and secret, makes
orders with a buyer link, takes a capture after the buyer "pays", then posts a signed PAYMENT.CAPTURE.COMPLETED
to the shop's webhook, and answers verify-webhook-signature. Its signature is an HMAC, not PayPal's real
certificate signature, and every page says MOCK. The real sandbox goes through PayPal's own API instead."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import secrets
import zlib
from datetime import datetime, timezone
from html import escape
from typing import Awaitable, Callable

import aiohttp
from aiohttp import web

from .paypal import MOCK_WEBHOOK_ID, basic_auth_ok

Deliver = Callable[[dict, bytes], Awaitable[None]]


def canonical(event: dict) -> bytes:
    return json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")


def signature(secret: bytes, transmission_id: str, when: str, webhook_id: str, body: bytes) -> str:
    msg = f"{transmission_id}|{when}|{webhook_id}|{zlib.crc32(body) & 0xFFFFFFFF}".encode()
    return base64.b64encode(hmac.new(secret, msg, hashlib.sha256).digest()).decode()


class MockPayPal:
    def __init__(self, webhook_url: str = "", client_id: str = "mock-client", client_secret: str = "mock-secret",
                 deliver: Deliver | None = None, delay: float = 0.4):
        self.client_id, self.client_secret = client_id, client_secret
        self.key = secrets.token_bytes(32)  # the mock's signing key; an attacker does not have it
        self.webhook_url = webhook_url
        self.deliver = deliver or self._post_to_webhook
        self.delay = delay
        self.orders: dict[str, dict] = {}
        self.sent: list[tuple[dict, bytes]] = []
        self._n = itertools.count(1)
        self.tasks: set[asyncio.Task] = set()

    # Events ---------------------------------------------------------------------------------------------
    def event(self, event_type: str, resource: dict, summary: str = "") -> dict:
        return {"id": f"WH-MOCK-{next(self._n):04d}-{secrets.token_hex(3).upper()}", "event_version": "1.0",
                "create_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "resource_type": "capture",
                "resource_version": "2.0", "event_type": event_type, "summary": summary or event_type,
                "resource": resource}

    def capture_event(self, order: dict, **over) -> dict:
        unit = order["unit"]
        resource = {"id": f"CAP-{order['id']}", "status": "COMPLETED", "final_capture": True,
                    "amount": dict(unit["amount"]), "custom_id": unit.get("custom_id", ""),
                    "invoice_id": unit.get("invoice_id", ""),
                    "supplementary_data": {"related_ids": {"order_id": order["id"]}}}
        resource.update(over)
        return self.event("PAYMENT.CAPTURE.COMPLETED", resource, "Payment completed")

    def sign(self, event: dict, webhook_id: str = MOCK_WEBHOOK_ID, key: bytes | None = None) -> tuple[dict, bytes]:
        """Headers and body the way PayPal would send them. `key` lets a test play an attacker with the wrong key."""
        body = canonical(event)
        tid, when = secrets.token_hex(8), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return ({"PAYPAL-AUTH-ALGO": "SHA256withRSA",
                 "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/MOCK",
                 "PAYPAL-TRANSMISSION-ID": tid, "PAYPAL-TRANSMISSION-TIME": when,
                 "PAYPAL-TRANSMISSION-SIG": signature(key or self.key, tid, when, webhook_id, body),
                 "Content-Type": "application/json"}, body)

    async def _post_to_webhook(self, headers: dict, body: bytes) -> None:
        if not self.webhook_url:
            return
        async with aiohttp.ClientSession() as http:
            async with http.post(self.webhook_url, data=body, headers=headers) as r:
                await r.read()

    async def send(self, event: dict) -> None:
        headers, body = self.sign(event)
        self.sent.append((headers, body))
        await asyncio.sleep(self.delay)  # real PayPal sends a moment after the capture
        await self.deliver(headers, body)

    def _later(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # Routes ---------------------------------------------------------------------------------------------
    async def token(self, request: web.Request) -> web.Response:
        if not basic_auth_ok(request.headers.get("Authorization", ""), self.client_id, self.client_secret):
            return web.json_response({"error": "invalid_client"}, status=401)
        return web.json_response({"access_token": "MOCK-TOKEN", "token_type": "Bearer", "expires_in": 32000})

    def _authed(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == "Bearer MOCK-TOKEN"

    async def create_order(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"name": "AUTHENTICATION_FAILURE"}, status=401)
        data = await request.json()
        unit = (data.get("purchase_units") or [{}])[0]
        key = request.headers.get("PayPal-Request-Id", "")
        for o in self.orders.values():  # idempotent: same request id, same order
            if key and o["key"] == key:
                return self._order_json(request, o)
        oid = "MOCK" + secrets.token_hex(7).upper()
        ctx = ((data.get("payment_source") or {}).get("paypal") or {}).get("experience_context") or {}
        order = {"id": oid, "key": key, "unit": unit, "status": "PAYER_ACTION_REQUIRED",
                 "return_url": ctx.get("return_url", ""), "cancel_url": ctx.get("cancel_url", "")}
        self.orders[oid] = order
        return self._order_json(request, order, 201)

    def _order_json(self, request: web.Request, order: dict, status: int = 200) -> web.Response:
        base = f"{request.scheme}://{request.host}/mock-paypal"
        return web.json_response({"id": order["id"], "status": order["status"], "links": [
            {"href": f"{base}/v2/checkout/orders/{order['id']}", "rel": "self", "method": "GET"},
            {"href": f"{base}/checkout?token={order['id']}", "rel": "payer-action", "method": "GET"}]}, status=status)

    async def checkout_page(self, request: web.Request) -> web.Response:
        order = self.orders.get(request.query.get("token", ""))
        if not order:
            raise web.HTTPNotFound()
        amount = order["unit"]["amount"]
        body = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>MOCK PayPal checkout</title>
<body style="font:16px system-ui;max-width:420px;margin:40px auto;padding:0 16px">
<p style="background:#fff3cd;padding:8px 12px;border-radius:6px"><b>MOCK PayPal.</b> This stands in for PayPal's own page when no sandbox keys are set.</p>
<h2>Pay {escape(amount['currency_code'])} {escape(amount['value'])}</h2>
<p>{escape(order['unit'].get('description', ''))}<br>Order {escape(order['unit'].get('custom_id', ''))}</p>
<form method=post action="/mock-paypal/pay/{escape(order['id'])}"><button style="font:inherit;padding:10px 18px">Pay now as the sandbox buyer</button></form>"""
        return web.Response(text=body, content_type="text/html")

    async def pay(self, request: web.Request) -> web.Response:
        order = self.orders.get(request.match_info["oid"])
        if not order:
            raise web.HTTPNotFound()
        order["status"] = "APPROVED"
        sep = "&" if "?" in order["return_url"] else "?"
        raise web.HTTPSeeOther(f"{order['return_url']}{sep}token={order['id']}&PayerID=MOCKBUYER")

    async def capture(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"name": "AUTHENTICATION_FAILURE"}, status=401)
        order = self.orders.get(request.match_info["oid"])
        if not order:
            return web.json_response({"name": "RESOURCE_NOT_FOUND"}, status=404)
        if order["status"] == "APPROVED":
            order["status"] = "COMPLETED"
            self._later(self.send(self.capture_event(order)))
        elif order["status"] != "COMPLETED":
            return web.json_response({"name": "ORDER_NOT_APPROVED"}, status=422)
        return web.json_response({"id": order["id"], "status": order["status"]}, status=201)

    async def verify(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"name": "AUTHENTICATION_FAILURE"}, status=401)
        try:
            data = await request.json()
            body = canonical(data["webhook_event"])
            want = signature(self.key, data["transmission_id"], data["transmission_time"], data["webhook_id"], body)
            ok = data["webhook_id"] == MOCK_WEBHOOK_ID and hmac.compare_digest(want, str(data["transmission_sig"]))
        except (KeyError, TypeError, ValueError):
            ok = False
        return web.json_response({"verification_status": "SUCCESS" if ok else "FAILURE"})

    def routes(self, app: web.Application) -> None:
        r = app.router
        r.add_post("/mock-paypal/v1/oauth2/token", self.token)
        r.add_post("/mock-paypal/v2/checkout/orders", self.create_order)
        r.add_post("/mock-paypal/v2/checkout/orders/{oid}/capture", self.capture)
        r.add_post("/mock-paypal/v1/notifications/verify-webhook-signature", self.verify)
        r.add_get("/mock-paypal/checkout", self.checkout_page)
        r.add_post("/mock-paypal/pay/{oid}", self.pay)
