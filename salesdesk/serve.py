"""Runs the web server (screens, PayPal webhook, buyer pages), Telegram and the follow-up clock on one event loop."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from .brain import make_brain
from .channels.instagram import InstagramSender
from .channels.mail import EmailSender
from .channels.whatsapp import WhatsAppSender
from .config import Settings
from .deliver import Dispatcher
from .desk import Desk
from .paypal import PayPalClient
from .senses import Senses
from .shop import load_shop
from .store import Store
from .web import Web

log = logging.getLogger("salesdesk.serve")


async def serve(settings: Settings) -> None:
    settings.check_paypal()
    store = Store(settings.db_path)
    shop = load_shop(settings.shop_dir)
    paypal = PayPalClient.from_settings(settings)
    desk = Desk(settings, store, shop, make_brain(settings), senses=Senses.from_settings(settings), paypal=paypal)
    if settings.demo_tools and not store.customers():
        from .seed import seed

        seed(desk)
    wa = WhatsAppSender(settings.whatsapp_token, settings.whatsapp_phone_number_id, settings.whatsapp_graph_version,
                        settings.whatsapp_template)
    ig = InstagramSender(settings.instagram_token, settings.instagram_user_id)
    mail = EmailSender(settings.smtp_host, settings.smtp_port, settings.smtp_user, settings.smtp_password,
                       settings.smtp_from, shop.name)
    dispatcher = Dispatcher(store, wa if wa.ready else None, ig if ig.ready else None, mail if mail.ready else None,
                            None, settings.owner_chat_id)
    stops = []
    try:
        if settings.telegram_token:
            from .channels.telegram_bot import build

            tg = build(settings.telegram_token, settings.owner_chat_id, desk, dispatcher)
            await tg.initialize()
            await tg.updater.start_polling()
            await tg.start()
            dispatcher.telegram = tg.bot
            log.info("Telegram: @%s", tg.bot.username)

            async def stop_tg() -> None:
                await tg.updater.stop()
                await tg.stop()
                await tg.shutdown()
            stops.append(stop_tg)

        runner = web.AppRunner(Web(settings, desk, dispatcher).app())
        await runner.setup()
        await web.TCPSite(runner, settings.host, settings.port).start()
        stops.append(runner.cleanup)
        log.info("%s on %s. Brief: http://%s:%s/  Board: /board  Phone: /phone", shop.name, settings.active_model,
                 settings.host, settings.port)
        if settings.paypal_mock:
            log.info("PayPal: MOCK (no PAYPAL_CLIENT_ID / PAYPAL_SECRET set). The built-in mock PayPal runs at /mock-paypal.")
        else:
            log.info("PayPal: %s. Webhook: %s/paypal/webhook (Webhook ID %s)", settings.paypal_env, settings.site_url,
                     "set" if settings.paypal_webhook_id else "MISSING: set PAYPAL_WEBHOOK_ID, no payment can be confirmed without it")
        log.info("Meta: voice notes and receipts %s. WhatsApp: %s. Instagram: %s. Email replies: %s.",
                 "on" if desk.senses.ready else "off (set META_API_KEY)", "on" if wa.ready else "off",
                 "on" if ig.ready else "off", "on" if mail.ready else "off")
        while True:  # the clock: follow-ups and the morning brief
            try:
                await dispatcher.deliver(desk.tick())
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(30)
    finally:
        for stop in reversed(stops):
            try:
                await stop()
            except Exception as exc:  # pragma: no cover
                log.warning("shutdown: %s", exc)
        await paypal.aclose()
        await wa.aclose()
        await ig.aclose()
        store.close()
