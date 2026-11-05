"""Command line: serve, chat in the terminal, print the brief, reset the demo, run the forged-receipt evaluation."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import load_settings


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="salesdesk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="web screens, PayPal webhook, Telegram and follow-ups")
    chat = sub.add_parser("chat", help="talk to the robot as a customer in the terminal")
    chat.add_argument("--channel", default="whatsapp")
    sub.add_parser("brief", help="print the morning brief")
    sub.add_parser("seed", help="reset the database to the demo shop's sample morning")
    ev = sub.add_parser("forged-eval", help="try 30 fake 'I've paid' receipts and messages; none may confirm an order")
    ev.add_argument("--out", default="EVAL.md", help="where to write the report")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.cmd == "forged-eval":
        for noisy in ("aiohttp.access", "salesdesk.deliver"):
            logging.getLogger(noisy).setLevel(logging.ERROR)
        from .forged_eval import run_eval

        result = asyncio.run(run_eval(Path(args.out), Path("evals") / "forged-receipts"))
        print(result.summary())
        sys.exit(0 if result.passed else 1)

    settings = load_settings()
    if args.cmd == "serve":
        from .serve import serve

        try:
            asyncio.run(serve(settings))
        except KeyboardInterrupt:
            pass
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
