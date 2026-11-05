"""HTTP: the owner's screens (brief, board), the demo phone, and webhooks for WhatsApp, Instagram,
forwarded emails, forwarded TikTok chats and forwarded bank alerts."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from pathlib import Path

from aiohttp import web

from . import ads as ads_mod
from . import ask as ask_mod
from . import views
from .shelf import ShelfCam
from .ads import AdBook
from .channels import instagram, whatsapp
from .channels.mail import parse_inbound
from .deliver import Dispatcher
from .desk import Desk
from .meta import MetaError, image_mime
from .payments import Alert, parse_alert
from .shop import naira

log = logging.getLogger("salesdesk.web")
PAGES = Path(__file__).with_name("pages")


def image_ext(data: bytes) -> str:
    return image_mime(data).split("/")[1].replace("jpeg", "jpg")


class Web:
    def __init__(self, settings, desk: Desk, dispatcher: Dispatcher, adbook: AdBook | None = None):
        self.settings, self.desk, self.dispatcher = settings, desk, dispatcher
        self.adbook = adbook or AdBook(settings)
        self.drafts = Path(settings.db_path).resolve().parent / "ad-drafts"  # Muse Image drafts, never posted anywhere
        self.tasks: set[asyncio.Task] = set()
        self._advising: asyncio.Task | None = None
        self.shelf = ShelfCam()

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
    async def _ad_report(self) -> dict:
        return ads_mod.report(self.desk, await self.adbook.ads(self.desk.now().date().isoformat()))

    async def api_brief(self, request: web.Request) -> web.Response:
        data = views.brief(self.desk)
        rep = await self._ad_report()
        data["ads"] = views.ads_card(rep, self.desk.store.ad_note(ads_mod.signature(rep)), self.adbook.live, self.adbook.error)
        return web.json_response(data)

    async def api_ad_notes(self, request: web.Request) -> web.Response:
        """Ask Muse about the ads (once per change in the numbers; the answer is cached)."""
        rep = await self._ad_report()
        if self._advising is None or self._advising.done():
            self._advising = asyncio.create_task(ads_mod.advise(self.desk, rep, self.adbook.client))
        notes = await asyncio.shield(self._advising)
        return web.json_response(views.ads_card(rep, notes, self.adbook.live, self.adbook.error))

    async def api_ad_picture(self, request: web.Request) -> web.Response:
        """Draft a new ad picture with Muse Image. It is saved for the owner to look at; nothing is posted."""
        client = self.adbook.client
        if not client:
            return web.json_response({"error": "Set META_API_KEY to draft pictures."}, status=400)
        rep = await self._ad_report()
        row = next((r for r in rep["rows"] if r["id"] == request.match_info["ad"]), None)
        if not row:
            raise web.HTTPNotFound()
        notes = self.desk.store.ad_note(ads_mod.signature(rep)) or {}
        prompt = ads_mod.picture_prompt(self.desk, row, (notes.get(row["id"]) or {}).get("picture", ""))
        try:
            image = await client.image(prompt)
        except MetaError as exc:
            log.warning("ad picture: %s", exc)
            return web.json_response({"error": "Muse Image couldn't draw it. Try again."}, status=502)
        product = ads_mod.ad_product(self.desk, row)
        if product:
            image = ads_mod.price_label(image, product.name, f"{naira(product.price)} a {product.unit}", self.desk.shop.name)
        self.drafts.mkdir(exist_ok=True)
        name = f"{row['id']}-{int(self.desk.now().timestamp())}.{image_ext(image)}"
        (self.drafts / name).write_bytes(image)
        return web.json_response({"url": "/ad-drafts/" + name, "prompt": prompt})

    async def api_ask(self, request: web.Request) -> web.Response:
        """The owner asks about her shop: a voice recording (multipart "file") or typed JSON {"text"}."""
        question = ""
        if request.content_type.startswith("multipart/"):
            form = await request.post()
            upload = form.get("file")
            audio = upload.file.read(20_000_000) if upload is not None and hasattr(upload, "file") else b""
            senses = self.desk.senses
            if not audio or not (senses and senses.ready):
                return web.json_response({"error": "Voice needs META_API_KEY. Type the question instead."}, status=400)
            try:
                question = await senses.hear(audio, ["ads", "ad"] + self.desk.shop.keywords())
            except MetaError as exc:
                log.warning("ask voice: %s", exc)
                return web.json_response({"error": "Couldn't hear that. Try again."}, status=502)
        else:
            question = str((await self._body(request)).get("text") or "")
        if not question.strip():
            return web.json_response({"error": "Couldn't hear a question. Try again."}, status=400)
        return web.json_response(await ask_mod.answer(self.desk, await self._ad_report(), self.adbook.client, question))

    async def api_shelf(self, request: web.Request) -> web.Response:
        return web.json_response(self.shelf.report(self.desk))

    async def api_shelf_snap(self, request: web.Request) -> web.Response:
        """Count a shelf frame with SAM 3.1: an uploaded photo (multipart "file") or, on the demo, {"sample"}."""
        client = self.adbook.client
        if not client:
            return web.json_response({"error": "Set META_API_KEY to count the shelf."}, status=400)
        sample = ""
        if request.content_type.startswith("multipart/"):
            upload = (await request.post()).get("file")
            image = upload.file.read(15_000_000) if upload is not None and hasattr(upload, "file") else b""
        else:
            sample = str((await self._body(request)).get("sample") or "")
            path = self.settings.shop_dir / "shelf" / f"{sample}.jpg"
            if not self.settings.demo_tools or sample not in ("before", "after") or not path.is_file():
                raise web.HTTPNotFound()
            image = path.read_bytes()
        if not image:
            return web.json_response({"error": "No photo."}, status=400)
        self.drafts.mkdir(exist_ok=True)
        now = self.desk.now()
        if sample == "before" and now.hour >= 9:
            now = now.replace(hour=9, minute=0, second=0, microsecond=0)  # the sample opening-time frame
        name = f"shelf-{int(now.timestamp())}-{len(self.shelf.snaps)}.{image_ext(image)}"
        (self.drafts / name).write_bytes(image)
        try:
            await self.shelf.snap(client, image, now, "/ad-drafts/" + name)
        except MetaError as exc:
            log.warning("shelf: %s", exc)
            return web.json_response({"error": "SAM 3.1 couldn't count that. Try again."}, status=502)
        return web.json_response(self.shelf.report(self.desk))

    async def ad_draft(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        path = self.drafts / name
        if "/" in name or "\\" in name or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

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
        if act == "confirm":
            await self.dispatcher.deliver(self.desk.confirm_payment(oid))
        elif act == "not-seen":
            await self.dispatcher.deliver(self.desk.payment_not_seen(oid))
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

    async def api_alert(self, request: web.Request) -> web.Response:
        aid, act = int(request.match_info["aid"]), request.match_info["act"]
        data = await self._body(request)
        if act == "confirm":
            await self.dispatcher.deliver(self.desk.alert_confirm(aid, data.get("order_id")))
        elif act == "dismiss":
            self.desk.alert_dismiss(aid)
        else:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def api_tick(self, request: web.Request) -> web.Response:
        await self.dispatcher.deliver(self.desk.tick())
        return web.json_response({"ok": True})

    # Demo tools --------------------------------------------------------------------------------
    def _demo_ad(self, ad_id: str) -> dict | None:
        """The demo phone's "came from this ad" choice, shaped like a real WhatsApp referral."""
        ad = next((a for a in self.adbook.cached() if a.id == ad_id), None)
        return {"id": ad.id, "name": ad.name, "click": "demo-click"} if ad else None

    async def demo_message(self, request: web.Request) -> web.Response:
        data = await self._body(request)
        channel = str(data.get("channel") or "whatsapp")
        address = "demo-" + "".join(ch for ch in str(data.get("address") or "phone") if ch.isalnum())[:30]
        outcome = await self.desk.handle(channel, address, str(data.get("text") or ""), str(data.get("name") or ""),
                                         self._demo_ad(str(data.get("ad") or "")))
        await self.dispatcher.deliver(outcome)
        return web.json_response(self._inbox(channel, address))

    async def demo_media(self, request: web.Request) -> web.Response:
        """A voice note or photo from the demo phone (multipart: channel, address, kind, ad, file)."""
        form = await request.post()
        channel = str(form.get("channel") or "whatsapp")
        address = "demo-" + "".join(ch for ch in str(form.get("address") or "phone") if ch.isalnum())[:30]
        kind = "audio" if form.get("kind") == "audio" else "image"
        upload = form.get("file")
        data = upload.file.read(20_000_000) if upload is not None and hasattr(upload, "file") else b""
        outcome = await self.desk.handle_media(channel, address, kind, data, ad=self._demo_ad(str(form.get("ad") or "")))
        await self.dispatcher.deliver(outcome)
        return web.json_response(self._inbox(channel, address))

    async def demo_ads(self, request: web.Request) -> web.Response:
        ads = await self.adbook.ads(self.desk.now().date().isoformat())
        return web.json_response({"ads": [{"id": a.id, "name": a.name} for a in ads], "senses": bool(self.desk.senses and self.desk.senses.ready)})

    async def demo_inbox(self, request: web.Request) -> web.Response:
        channel = request.query.get("channel", "whatsapp")
        address = "demo-" + "".join(ch for ch in request.query.get("address", "phone") if ch.isalnum())[:30]
        return web.json_response(self._inbox(channel, address))

    def _inbox(self, channel: str, address: str) -> dict:
        box = self.dispatcher.demo_inbox[f"{channel}:{address}"]
        replies = list(box)
        box.clear()
        d = self.dispatcher  # what the owner was sent: the last few notes, and how many there have been
        return {"replies": replies, "owner": list(d.owner_log)[-3:], "owner_n": d.owner_count}

    async def demo_alert(self, request: web.Request) -> web.Response:
        """A pretend bank alert, so the matching can be shown without a real bank email."""
        data = await self._body(request)
        try:
            amount = int(str(data.get("amount") or "0").replace(",", "").replace("₦", "").strip() or 0)
        except ValueError:
            amount = 0
        if amount <= 0:
            return web.json_response({"error": "amount"}, status=400)
        verified = bool(data.get("verified", True))
        n = len(self.desk.store.alerts(("confirmed", "to_check", "unmatched", "owner_confirmed", "dismissed")))
        alert = Alert(f"demo-{n + 1}-{amount}", amount, str(data.get("sender") or "")[:80], str(data.get("narration") or "")[:120],
                      "demo bank", verified, "test alert marked as signed" if verified else "test alert marked as not signed")
        await self.dispatcher.deliver(self.desk.bank_alert(alert))
        return web.json_response({"ok": True})

    async def demo_reset(self, request: web.Request) -> web.Response:
        from .seed import seed

        seed(self.desk)
        self.dispatcher.demo_inbox.clear()
        self.shelf.snaps.clear()
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

    async def inbound_alert(self, request: web.Request) -> web.Response:
        """A bank alert email, raw (RFC 822), forwarded with its original headers."""
        if not self._token_ok(request):
            return web.Response(status=403)
        alert = parse_alert(await request.read(), self.settings.alert_domains)
        if alert:
            await self.dispatcher.deliver(self.desk.bank_alert(alert))
        return web.json_response({"credit_alert": bool(alert), "verified": bool(alert and alert.verified)})

    async def inbound_chat(self, request: web.Request) -> web.Response:
        """A chat forwarded by a tool such as ManyChat or Make: JSON {sender, name, text}. Used for TikTok,
        whose messaging API is invite-only; replies are left for the owner to send in the app."""
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
        if key and (request.path in ("/", "/board", "/phone", "/shelf") or request.path.startswith(("/api/", "/ad-drafts/"))):
            given = request.query.get("k") or request.headers.get("X-Demo-Key", "")
            if not hmac.compare_digest(given.encode(), key.encode()):
                return web.Response(status=404, text="Not found")
        return await handler(request)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.demo_lock])
        r = app.router
        r.add_get("/", self._page("brief.html"))
        r.add_get("/board", self._page("board.html"))
        r.add_get("/shelf", self._page("shelf.html"))
        r.add_get("/app.css", self.css)
        r.add_get("/api/brief", self.api_brief)
        r.add_get("/api/board", self.api_board)
        r.add_get("/api/chat/{cid:\\d+}", self.api_chat)
        r.add_post("/api/order/{oid:\\d+}/{act}", self.api_order)
        r.add_post("/api/customer/{cid:\\d+}/{act}", self.api_customer)
        r.add_post("/api/alert/{aid:\\d+}/{act}", self.api_alert)
        r.add_post("/api/tick", self.api_tick)
        r.add_post("/api/ads/notes", self.api_ad_notes)
        r.add_post("/api/ask", self.api_ask)
        r.add_get("/api/shelf", self.api_shelf)
        r.add_post("/api/shelf/snap", self.api_shelf_snap)
        r.add_post("/api/ads/{ad}/picture", self.api_ad_picture)
        r.add_get("/ad-drafts/{name}", self.ad_draft)
        r.add_get("/webhook/whatsapp", self.wa_verify)
        r.add_post("/webhook/whatsapp", self.wa_receive)
        r.add_get("/webhook/instagram", self.ig_verify)
        r.add_post("/webhook/instagram", self.ig_receive)
        r.add_post("/inbound/email", self.inbound_email)
        r.add_post("/inbound/bank-alert", self.inbound_alert)
        r.add_post("/inbound/{channel}", self.inbound_chat)
        r.add_get("/health", self.health)
        if self.settings.demo_tools:
            r.add_get("/phone", self._page("phone.html"))
            r.add_post("/api/demo/message", self.demo_message)
            r.add_post("/api/demo/media", self.demo_media)
            r.add_get("/api/demo/ads", self.demo_ads)
            r.add_get("/api/demo/inbox", self.demo_inbox)
            r.add_post("/api/demo/bank-alert", self.demo_alert)
            r.add_post("/api/demo/reset", self.demo_reset)
        return app
