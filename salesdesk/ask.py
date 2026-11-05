"""Ask Sales Desk: the owner asks about her own shop, by voice or typing, and gets a short spoken answer.

Muse Voice Transcribe turns her voice into text. Muse Spark answers from this morning's brief, the ad report and
the price list only. Any naira amount in the answer that isn't in those facts is thrown out, and she gets the
plain brief headline instead. Nothing here changes an order, an ad or a price."""

from __future__ import annotations

import logging

from . import views
from .desk import AMOUNT
from .meta import MetaClient, MetaError
from .shop import naira

log = logging.getLogger("salesdesk.ask")

ASK_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "description": "The spoken answer, at most 3 short sentences."}},
    "required": ["answer"], "additionalProperties": False,
}

ASK_SYSTEM = """You are Sales Desk, the sales assistant for {shop} in {city}, Nigeria. {owner}, the owner, is asking \
about her own shop. Answer in at most 3 short sentences that sound natural read aloud: friendly Nigerian English, \
simple words, no lists, no jargon like ROAS or CTR. Talk to her directly, \
in the language she asked in (Pidgin only if she asked in Pidgin).
- Use only what is in <brief>, <ads> and <facts>. If the answer isn't there, say you can't see that yet.
- Only use naira amounts that appear there, written with the ₦ sign. Never invent numbers or customers.
- When she asks what to do, give one clear next step from the facts. Don't say you changed anything; she decides."""


def facts(desk, rep: dict) -> tuple[str, set[int]]:
    """The text Muse may answer from, and every naira amount in it."""
    b = views.brief(desk)
    lines = [f"{b['greeting']}, {b['owner']}. {b['headline']}", f"Sold since yesterday: {b['sold']}.",
             f"Transfers waiting to be checked: {b['waiting']}."]
    lines += [f"Needs you: {n['title']}. {n['detail']}" for n in b["needs"][:8]]
    lines += [f"Follow-up today at {f['time']}: {f['name']}, {f['about']}" + (" (skipped)" if f["skipped"] else "")
              for f in b["follows"][:8]]
    lines += [f"{c['name']}: {c['chats']} chats, {c['sold']}" for c in b["channels"]]
    ads = [f"{r['name']}: spent {naira(r['spend'])}, {r['chats']} chats, {r['paid']} paid orders, "
           f"{naira(r['back'])} back, {r['haggled']} asked for a lower price, status {r['verdict']}" for r in rep["rows"]]
    if rep["rows"]:
        ads.append(f"All ads: spent {naira(rep['spend'])}, {naira(rep['back'])} back from {rep['paid']} paid orders.")
    text = (f"<brief>\n" + "\n".join(lines) + "\n</brief>\n<ads>\n" + ("\n".join(ads) or "No ads yesterday.")
            + f"\n</ads>\n<facts>\n{desk.shop.facts()}\n</facts>")
    allowed = desk.shop.prices() | {int((m.group(1) or m.group(2)).replace(",", "")) for m in AMOUNT.finditer(text)}
    return text, allowed


def fallback(desk) -> str:
    b = views.brief(desk)
    return f"{b['headline']} You've sold {b['sold']} since yesterday."


async def answer(desk, rep: dict, client: MetaClient | None, question: str) -> dict:
    """{"question", "answer", "by"}: by is the model id, or "brief" when the plain headline was used."""
    question = " ".join(question.split())[:400]
    if not client or not question:
        return {"question": question, "answer": fallback(desk), "by": "brief"}
    text, allowed = facts(desk, rep)
    shop = desk.shop
    try:
        data, model = await client.chat_json(ASK_SYSTEM.format(shop=shop.name, city=shop.city, owner=shop.owner),
                                             f"{text}\n<question>{question}</question>", ASK_SCHEMA, "ask_answer",
                                             max_tokens=800)
    except MetaError as exc:
        log.warning("ask: %s", exc)
        return {"question": question, "answer": fallback(desk), "by": "brief"}
    said = " ".join(str(data.get("answer") or "").split())
    ok = said and all(int((m.group(1) or m.group(2)).replace(",", "")) in allowed for m in AMOUNT.finditer(said))
    if not ok:
        log.warning("ask: answer dropped (empty or an unknown naira amount): %r", said[:300])
        return {"question": question, "answer": fallback(desk), "by": "brief"}
    return {"question": question, "answer": said[:600], "by": model}
