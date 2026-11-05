"""HTTP: the owner's screens (brief, board), the demo phone, the PayPal webhook and buyer pages, and the
webhooks for WhatsApp, Instagram, forwarded emails and forwarded TikTok chats."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from html import escape
from pathlib import Path

from aiohttp import web

from . import forge, views
from .channels import instagram, whatsapp
from .channels.mail import parse_inbound
from .deliver import Dispatcher
from .desk import Desk
from .mockpaypal import MockPayPal
from .paypal import PayPalClient, PayPalError

log = logging.getLogger("salesdesk.web")
PAGES = Path(__file__).with_name("pages")


class Web:
    def __init__(self, settings, desk: Desk, dispatcher: Dispatcher, mock: MockPayPal | None = None):
        self.settings, self.desk, self.dispatcher = settings, desk, dispatcher
        self.tasks: set[asyncio.Task] = set()
        self.mock = mock
        if self.mock is None and settings.paypal_mock:
            self.mock = MockPayPal(client_id="mock-client", client_secret="mock-secret")
        if self.mock is not None:
            self.mock.deliver = self._mock_delivers  # the mock PayPal posts its signed events to our own webhook code

    def _later(self, coro) -> None:
        """Answer the webhook fast, then do the slow work."""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(self, coro) -> None:
        try:
            await self.dispatcher.deliver(await coro)
        except Exception:
            log.exception("handling failed")

    def _token_ok(self, request: web.Request) -> bool:
        token = self.settings.inbound_token
        return bool(token) and hmac.compare_digest(request.query.get("token", "").encode(), token.encode())

    # Pages ------------------------------------------------------------------------------------
    def _page(self, name: str):
        async def handler(request: web.Request) -> web.Response:
            return web.Response(text=(PAGES / name).read_text(encoding="utf-8"), content_type="text/html")
        return handler

    async def css(self, request: web.Request) -> web.Response:
        return web.Response(text=(PAGES / "app.css").read_text(encoding="utf-8"), content_type="text/css")

    # The owner's API ---------------------------------------------------------------------------
    async def api_brief(self, request: web.Request) -> web.Response:
        return web.json_response(views.brief(self.desk))

    async def api_board(self, request: web.Request) -> web.Response:
        return web.json_response(views.board(self.desk))

    async def api_chat(self, request: web.Request) -> web.Response:
        data = views.chat(self.desk, int(request.match_info["cid"]))
        return web.json_response(data) if data else web.json_response({"error": "not found"}, status=404)

    async def _body(self, request: web.Request) -> dict:
        try:
            return await request.json()
        except (json.JSONDecodeError, ValueError):
            return {}

    async def api_order(self, request: web.Request) -> web.Response:
        oid, act = int(request.match_info["oid"]), request.match_info["act"]
        data = await self._body(request)
        if act == "approve":  # the owner's OK is what lets the robot make the PayPal link
            await self.dispatcher.deliver(await self.desk.approve_payment(oid))
        elif act == "delivered":
            await self.dispatcher.deliver(self.desk.mark_delivered(oid))
        elif act == "skip":
            self.desk.skip_follow_up(oid, bool(data.get("skip", True)))
        else:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def api_customer(self, request: web.Request) -> web.Response:
        cid, act = int(request.match_info["cid"]), request.match_info["act"]
        data = await self._body(request)
        if act == "reply":
            outcome = self.desk.owner_reply(cid, str(data.get("text") or ""))
            await self.dispatcher.deliver(outcome)
            return web.json_response({"sent": bool(outcome.replies)})
        if act == "takeover":
            self.desk.set_takeover(cid, bool(data.get("on", True)))
        elif act == "robot":
            self.desk.robot_handles(cid)
        else:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def api_tick(self, request: web.Request) -> web.Response:
        await self.dispatcher.deliver(self.desk.tick())
        return web.json_response({"ok": True})

    # PayPal ------------------------------------------------------------------------------------
    async def _webhook(self, headers, body: bytes) -> tuple[int, str]:
        """The one door money comes through. Anything PayPal itself doesn't vouch for is refused with 401 and
        changes nothing. A genuine event is answered 200 even when the desk then declines to use it, so PayPal
        doesn't keep retrying."""
        client = self.desk.paypal
        if client is None:
            return 503, "PayPal isn't set up"
        event = await client.verify(headers, body)
        if event is None:
            log.warning("PayPal webhook refused: signature not verified")
            return 401, "signature not verified"
        await self.dispatcher.deliver(self.desk.paypal_event(event))
        return 200, "ok"

    async def paypal_webhook(self, request: web.Request) -> web.Response:
        status, text = await self._webhook(request.headers, await request.read())
        return web.Response(status=status, text=text)

    async def _mock_delivers(self, headers: dict, body: bytes) -> None:
        await self._webhook(headers, body)

    @staticmethod
    def _buyer_page(title: str, text: str) -> web.Response:
        return web.Response(content_type="text/html", text=f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{escape(title)}</title>
<body style="font:17px/1.5 system-ui;max-width:440px;margin:56px auto;padding:0 18px"><h2>{escape(title)}</h2><p>{escape(text)}</p>""")

    async def paypal_return(self, request: web.Request) -> web.Response:
        """The buyer's browser comes back from PayPal. This collects the money, but never marks an order Paid:
        only the signed webhook does that."""
        token = request.query.get("token", "")
        order = self.desk.store.order_by_paypal_id(token)
        if not order or not self.desk.paypal:
            raise web.HTTPNotFound()
        try:
            await self.desk.paypal.capture(token)
        except PayPalError as exc:
            log.warning("capture: %s", exc)
            return self._buyer_page("Something went wrong", "PayPal could not finish the payment. Nothing was taken "
                                    "unless PayPal says so. Message the shop and we'll sort it out.")
        self.desk.buyer_returned(token)
        return self._buyer_page("Thank you", f"PayPal is confirming your payment. {self.desk.shop.name} will message you in the chat "
                                "the moment it arrives. You can close this page.")

    async def paypal_cancel(self, request: web.Request) -> web.Response:
        return self._buyer_page("Payment cancelled", "Nothing was taken. Go back to the chat when you're ready to pay.")

    # Demo tools --------------------------------------------------------------------------------
    @staticmethod
    def _demo_address(raw: str) -> str:
        return "demo-" + "".join(ch for ch in str(raw or "phone") if ch.isalnum())[:30]

    async def demo_message(self, request: web.Request) -> web.Response:
        data = await self._body(request)
        channel = str(data.get("channel") or "whatsapp")
        address = self._demo_address(data.get("address"))
        outcome = await self.desk.handle(channel, address, str(data.get("text") or ""), str(data.get("name") or ""))
        await self.dispatcher.deliver(outcome)
        return web.json_response(self._inbox(channel, address))

    async def demo_media(self, request: web.Request) -> web.Response:
        """A voice note or photo from the demo phone (multipart: channel, address, kind, file)."""
        form = await request.post()
        channel = str(form.get("channel") or "whatsapp")
        address = self._demo_address(form.get("address"))
        kind = "audio" if form.get("kind") == "audio" else "image"
        upload = form.get("file")
        data = upload.file.read(20_000_000) if upload is not None and hasattr(upload, "file") else b""
        outcome = await self.desk.handle_media(channel, address, kind, data)
        await self.dispatcher.deliver(outcome)
        return web.json_response(self._inbox(channel, address))

    async def demo_inbox(self, request: web.Request) -> web.Response:
        channel = request.query.get("channel", "whatsapp")
        return web.json_response(self._inbox(channel, self._demo_address(request.query.get("address", "phone"))))

    def _inbox(self, channel: str, address: str) -> dict:
        box = self.dispatcher.demo_inbox[f"{channel}:{address}"]
        replies = list(box)
        box.clear()
        d = self.dispatcher  # what the owner was sent: the last few notes, and how many there have been
        return {"replies": replies, "owner": list(d.owner_log)[-3:], "owner_n": d.owner_count}

    async def demo_forge(self, request: web.Request) -> web.Response:
        """Send a forged PayPal message to the real webhook. It goes through the same door as a genuine one, and
        has to be refused there; the demo shows the order staying 'Awaiting PayPal'."""
        data = await self._body(request)
        kind = str(data.get("kind") or "")
        if data.get("address"):  # from the demo phone: the order that customer is paying for
            cust = self.desk.store.customer(str(data.get("channel") or "whatsapp"), self._demo_address(data["address"]), self.desk.now())
            order = self.desk.store.open_order(cust["id"])
        else:
            order = self.desk.store.get_order(int(data.get("order_id") or 0))
        if kind not in forge.KINDS or not order or not order["pp_order_id"] or self.mock is None:
            return web.json_response({"error": "Needs an order with a PayPal link, and the built-in mock PayPal."}, status=400)
        headers, body = forge.forged(kind, self.mock, order["pp_order_id"], self.desk.code(order["id"]),
                                     f"{order['total']:.2f}", self.desk.shop.currency)
        status, text = await self._webhook(headers, body)
        fresh = self.desk.store.get_order(order["id"])
        return web.json_response({"status": status, "answer": text, "kind": kind, "stage": fresh["stage"],
                                  "refused": status != 200})

    async def demo_reset(self, request: web.Request) -> web.Response:
        from .seed import seed

        seed(self.desk)
        self.dispatcher.demo_inbox.clear()
        if self.mock is not None:
            self.mock.orders.clear()
        return web.json_response({"ok": True})

    # Webhooks ----------------------------------------------------------------------------------
    async def wa_verify(self, request: web.Request) -> web.Response:
        q, s = request.query, self.settings
        if q.get("hub.mode") == "subscribe" and s.whatsapp_verify_token and q.get("hub.verify_token") == s.whatsapp_verify_token:
            return web.Response(text=q.get("hub.challenge", ""))
        return web.Response(status=403)

    async def wa_receive(self, request: web.Request) -> web.Response:
        body = await request.read()
        if not whatsapp.valid_signature(self.settings.whatsapp_app_secret, body, request.headers.get("X-Hub-Signature-256")):
            return web.Response(status=401)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return web.Response(status=400)
        for sender, profile, _msg_id, text, extra in whatsapp.incoming(payload):
            media = extra["media"]
            if media and media["id"] and self.dispatcher.whatsapp:
                self._later(self._run(self._wa_media(sender, profile, media, extra["ad"], text)))
            else:
                self._later(self._run(self.desk.handle("whatsapp", sender, text, profile, extra["ad"])))
        return web.Response(text="ok")

    async def _wa_media(self, sender: str, profile: str, media: dict, ad: dict | None, text: str):
        try:
            data = await self.dispatcher.whatsapp.download(media["id"])
        except Exception as exc:  # a failed download still reaches the robot as a placeholder
            log.warning("WhatsApp media download failed: %s", exc)
            return await self.desk.handle("whatsapp", sender, text, profile, ad)
        caption = "" if text.startswith("[sent a") else text
        return await self.desk.handle_media("whatsapp", sender, media["kind"], data, profile, ad, caption)

    async def ig_verify(self, request: web.Request) -> web.Response:
        q, s = request.query, self.settings
        if q.get("hub.mode") == "subscribe" and s.instagram_verify_token and q.get("hub.verify_token") == s.instagram_verify_token:
            return web.Response(text=q.get("hub.challenge", ""))
        return web.Response(status=403)

    async def ig_receive(self, request: web.Request) -> web.Response:
        body = await request.read()
        if not instagram.valid_signature(self.settings.instagram_app_secret, body, request.headers.get("X-Hub-Signature-256")):
            return web.Response(status=401)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return web.Response(status=400)
        for sender, text, ad in instagram.incoming(payload, self.settings.instagram_user_id):
            self._later(self._run(self.desk.handle("instagram", sender, text, ad=ad)))
        return web.Response(text="ok")

    async def inbound_email(self, request: web.Request) -> web.Response:
        """A customer email, raw (RFC 822), from an email forwarder such as a Cloudflare Email Worker."""
        if not self._token_ok(request):
            return web.Response(status=403)
        addr, name, text = parse_inbound(await request.read())
        if addr and text:
            self._later(self._run(self.desk.handle("email", addr, text, name)))
        return web.json_response({"ok": bool(addr and text)})

    async def inbound_chat(self, request: web.Request) -> web.Response:
        """A chat forwarded by a tool such as ManyChat or Make: JSON {sender, name, text}."""
        if not self._token_ok(request):
            return web.Response(status=403)
        channel = request.match_info["channel"]
        if channel not in ("tiktok", "facebook", "website"):
            raise web.HTTPNotFound()
        data = await self._body(request)
        sender, text = str(data.get("sender") or "").strip(), str(data.get("text") or "").strip()
        if sender and text:
            self._later(self._run(self.desk.handle(channel, sender, text, str(data.get("name") or ""))))
        return web.json_response({"ok": bool(sender and text)})

    async def health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    @web.middleware
    async def demo_lock(self, request: web.Request, handler):
        """With DEMO_KEY set, every screen and /api/ call answers only links carrying the key."""
        key = self.settings.demo_key
        if key and (request.path in ("/", "/board", "/phone") or request.path.startswith("/api/")):
            given = request.query.get("k") or request.headers.get("X-Demo-Key", "")
            if not hmac.compare_digest(given.encode(), key.encode()):
                return web.Response(status=404, text="Not found")
        return await handler(request)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.demo_lock])
        r = app.router
        r.add_get("/", self._page("brief.html"))
        r.add_get("/board", self._page("board.html"))
        r.add_get("/app.css", self.css)
        r.add_get("/api/brief", self.api_brief)
        r.add_get("/api/board", self.api_board)
        r.add_get("/api/chat/{cid:\\d+}", self.api_chat)
        r.add_post("/api/order/{oid:\\d+}/{act}", self.api_order)
        r.add_post("/api/customer/{cid:\\d+}/{act}", self.api_customer)
        r.add_post("/api/tick", self.api_tick)
        r.add_post("/paypal/webhook", self.paypal_webhook)
        r.add_get("/paypal/return", self.paypal_return)
        r.add_get("/paypal/cancel", self.paypal_cancel)
        r.add_get("/webhook/whatsapp", self.wa_verify)
        r.add_post("/webhook/whatsapp", self.wa_receive)
        r.add_get("/webhook/instagram", self.ig_verify)
        r.add_post("/webhook/instagram", self.ig_receive)
        r.add_post("/inbound/email", self.inbound_email)
        r.add_post("/inbound/{channel}", self.inbound_chat)
        r.add_get("/health", self.health)
        if self.mock is not None:
            self.mock.routes(app)
        if self.settings.demo_tools:
            r.add_get("/phone", self._page("phone.html"))
            r.add_post("/api/demo/message", self.demo_message)
            r.add_post("/api/demo/media", self.demo_media)
            r.add_get("/api/demo/inbox", self.demo_inbox)
            r.add_post("/api/demo/forge", self.demo_forge)
            r.add_post("/api/demo/reset", self.demo_reset)
        return app
