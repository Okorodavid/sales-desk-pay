"""Command line: serve, chat in the terminal, check bank alerts, print the brief, reset the demo."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import timedelta
from pathlib import Path

from .config import load_settings


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="salesdesk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="web screens, webhooks, Telegram, follow-ups and the bank-alert reader")
    chat = sub.add_parser("chat", help="talk to the robot as a customer in the terminal")
    chat.add_argument("--channel", default="whatsapp")
    sub.add_parser("brief", help="print the morning brief")
    sub.add_parser("seed", help="reset the database to the demo shop's sample morning")
    alerts = sub.add_parser("alerts", help="read the mailbox once and match bank alerts to orders")
    alerts.add_argument("--days", type=int, default=2)
    parse = sub.add_parser("parse-alert", help="show how a saved bank email (.eml) would be read")
    parse.add_argument("file")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()

    if args.cmd == "serve":
        from .serve import serve

        try:
            asyncio.run(serve(settings))
        except KeyboardInterrupt:
            pass
        return
    if args.cmd == "parse-alert":
        from .payments import parse_alert

        alert = parse_alert(Path(args.file).read_bytes(), settings.alert_domains)
        print("Not a credit alert." if not alert else
              f"Credit alert: ₦{alert.amount:,} from {alert.sender or '?'}\nNarration: {alert.narration or '-'}\n"
              f"Verified: {'yes' if alert.verified else 'no'} ({alert.why})")
        return

    from .brain import make_brain
    from .desk import Desk
    from .shop import load_shop
    from .store import Store

    store = Store(settings.db_path)
    desk = Desk(settings, store, load_shop(settings.shop_dir), make_brain(settings))
    if args.cmd == "seed":
        from .seed import seed

        seed(desk)
        print("Demo shop reset.")
    elif args.cmd == "brief":
        from .views import brief_text

        print(brief_text(desk))
    elif args.cmd == "alerts":
        from .deliver import Dispatcher
        from .payments import MailboxReader
        from .serve import check_mailbox

        reader = MailboxReader(settings.imap_host, settings.imap_user, settings.imap_password, settings.imap_folder,
                               settings.alert_domains)
        if not reader.ready:
            sys.exit("Set IMAP_HOST, IMAP_USER and IMAP_PASSWORD first (see README).")
        disp = Dispatcher(store)
        n = asyncio.run(check_mailbox(reader, desk, disp, desk.now() - timedelta(days=args.days)))
        print(f"{n} new credit alert(s).")
        for note in disp.owner_log:
            print("-", note)
    elif args.cmd == "chat":
        asyncio.run(_chat(desk, args.channel))
    store.close()


async def _chat(desk, channel: str) -> None:
    print(f"Chatting with {desk.shop.name} as a {channel} customer. Ctrl+C to stop.")
    while True:
        try:
            text = await asyncio.to_thread(input, "you> ")
        except (EOFError, KeyboardInterrupt):
            return
        outcome = await desk.handle(channel, "demo-terminal", text)
        for r in outcome.replies:
            print("robot>", r.text)
        for n in outcome.notes:
            print("  [to the owner]", n.text.splitlines()[0])


if __name__ == "__main__":
    main()
