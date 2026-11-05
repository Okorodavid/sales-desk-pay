"""Runs the web server, Telegram, the follow-up clock and the bank-alert reader on one event loop."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiohttp import web

from .ads import AdBook
from .brain import make_brain
from .channels.instagram import InstagramSender
from .channels.mail import EmailSender
from .channels.whatsapp import WhatsAppSender
from .config import Settings
from .deliver import Dispatcher
from .desk import Desk
from .payments import MailboxReader, parse_alert
from .senses import Senses
from .shop import load_shop
from .store import Store
from .web import Web

log = logging.getLogger("salesdesk.serve")


async def check_mailbox(reader: MailboxReader, desk: Desk, dispatcher: Dispatcher, since) -> int:
    """Read new bank emails, match credit alerts to orders. Returns how many alerts were new."""
    raws = await asyncio.to_thread(reader.fetch, since)
    new = 0
    for raw in raws:
        alert = parse_alert(raw, reader.allowed)
        if alert and not desk.store.seen_alert(alert.msg_id):
            new += 1
            await dispatcher.deliver(desk.bank_alert(alert))
    return new


async def serve(settings: Settings) -> None:
    store = Store(settings.db_path)
    shop = load_shop(settings.shop_dir)
    desk = Desk(settings, store, shop, make_brain(settings), senses=Senses.from_settings(settings))
    adbook = AdBook.from_settings(settings)
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
    reader = MailboxReader(settings.imap_host, settings.imap_user, settings.imap_password, settings.imap_folder,
                           settings.alert_domains)
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

        runner = web.AppRunner(Web(settings, desk, dispatcher, adbook).app())
        await runner.setup()
        await web.TCPSite(runner, settings.host, settings.port).start()
        stops.append(runner.cleanup)
        log.info("%s on %s. Brief: http://%s:%s/", shop.name, settings.active_model, settings.host, settings.port)
        log.info("Meta: voice notes and receipts %s; ad spend from %s.", "on" if desk.senses.ready else "off (set META_API_KEY)",
                 "the Marketing API" if adbook.live else "the shop's ads.json sample")
        log.info("WhatsApp: %s. Instagram: %s. Email replies: %s. Bank alerts: %s (auto-confirm %s).",
                 "on" if wa.ready else "off", "on" if ig.ready else "off", "on" if mail.ready else "off",
                 "reading " + settings.imap_user if reader.ready else "off (set IMAP_*)", "on" if settings.auto_confirm else "off")

        since = desk.now() - timedelta(days=1)
        next_mail = desk.now()
        while True:  # the clock: follow-ups, the morning brief, the mailbox
            try:
                await dispatcher.deliver(desk.tick())
            except Exception:
                log.exception("tick failed")
            if reader.ready and desk.now() >= next_mail:
                next_mail = desk.now() + timedelta(minutes=settings.check_alerts_minutes)
                try:
                    n = await check_mailbox(reader, desk, dispatcher, since)
                    if n:
                        log.info("bank alerts: %s new", n)
                except Exception as exc:
                    log.warning("mailbox check failed: %s", exc)
            await asyncio.sleep(30)
    finally:
        for stop in reversed(stops):
            try:
                await stop()
            except Exception as exc:  # pragma: no cover
                log.warning("shutdown: %s", exc)
        await wa.aclose()
        await ig.aclose()
        store.close()
