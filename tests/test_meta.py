"""The Meta parts: ad referrals, the ad report and Muse's notes, voice notes, receipt photos, and the API clients.
No network: Meta's APIs are replaced by httpx mock transports or small fakes."""

import asyncio
import json

import httpx

from salesdesk import ads as ads_mod
from salesdesk import views
from salesdesk.brain import MuseBrain
from salesdesk.channels import instagram, whatsapp
from salesdesk.config import Settings
from salesdesk.meta import MetaClient
from salesdesk.seed import seed
from salesdesk.senses import Receipt

from conftest import ROOT

ANKARA, LACE, ASO = "120211000000000101", "120211000000000102", "120211000000000103"


def run(coro):
    return asyncio.run(coro)


def test_whatsapp_referral_and_voice_note():
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "2348011112222", "profile": {"name": "Ada"}}],
        "messages": [
            {"from": "2348011112222", "id": "m1", "type": "text", "text": {"body": "Is this lace available?"},
             "referral": {"source_type": "ad", "source_id": LACE, "headline": "Lace for owambe", "ctwa_clid": "ARAkLm"}},
            {"from": "2348011112222", "id": "m2", "type": "audio", "audio": {"id": "media-9", "mime_type": "audio/ogg; codecs=opus"}},
        ]}}]}]}
    (_, name, _, text, extra), (_, _, _, vtext, vextra) = whatsapp.incoming(payload)
    assert name == "Ada" and text == "Is this lace available?"
    assert extra["ad"] == {"id": LACE, "name": "Lace for owambe", "click": "ARAkLm"}
    assert vtext == "[sent a voice note]" and vextra["media"]["id"] == "media-9" and vextra["ad"] is None


def test_instagram_ad_referral():
    payload = {"object": "instagram", "entry": [{"messaging": [{
        "sender": {"id": "ig-55"}, "message": {"text": "Price?"},
        "referral": {"source": "ADS", "type": "OPEN_THREAD", "ad_id": ASO, "ref": "r1",
                     "ads_context_data": {"ad_title": "Aso-oke bridal set"}}}]}]}
    assert instagram.incoming(payload) == [("ig-55", "Price?", {"id": ASO, "name": "Aso-oke bridal set", "click": "r1"})]


def test_first_ad_keeps_the_credit(make_desk):
    d = make_desk()
    run(d.handle("whatsapp", "234801", "Hello", ad={"id": LACE, "name": "Lace for owambe", "click": "c1"}))
    run(d.handle("whatsapp", "234801", "Hello again", ad={"id": ANKARA, "name": "Ankara", "click": "c2"}))
    c = d.store.customer("whatsapp", "234801", d.now())
    assert (c["ad_id"], c["ad_name"], c["ad_click"]) == (LACE, "Lace for owambe", "c1")


def test_ad_report_from_the_sample_morning(make_desk):
    d = make_desk()
    seed(d)
    rep = ads_mod.report(d, ads_mod.sample_ads(ROOT / "demo"))
    rows = {r["id"]: r for r in rep["rows"]}
    lace, ankara, aso = rows[LACE], rows[ANKARA], rows[ASO]
    assert (lace["chats"], lace["paid"], lace["back"], lace["verdict"]) == (5, 0, 0, "losing")
    assert lace["haggled"] == 4  # last price, too much, best price, can you do 10k
    assert (ankara["chats"], ankara["paid"], ankara["back"]) == (3, 1, 44000)  # Ngozi paid; Chioma's transfer isn't in yet
    assert (aso["paid"], aso["back"], aso["per_naira"], aso["verdict"]) == (1, 87500, 35.0, "best")
    assert rep["rows"][0]["id"] == LACE  # money-losing ads come first
    assert (rep["spend"], rep["back"]) == (12500, 131500)
    card = views.ads_card(rep, None, live=False)
    assert card["notes_ready"] is False and card["spend"] == "₦12,500"
    assert {r["id"]: r["picture"] for r in card["rows"]} == {LACE: True, ANKARA: False, ASO: False}


class FakeMuse:
    def __init__(self, answer):
        self.answer, self.calls, self.seen = answer, 0, ""

    async def chat_json(self, system, text, schema, name, image=None, effort="low", max_tokens=1500):
        self.calls += 1
        self.seen = text
        return self.answer, "muse-spark-1.3"


def test_muse_notes_are_checked_and_cached(make_desk):
    d = make_desk()
    seed(d)
    rep = ads_mod.report(d, ads_mod.sample_ads(ROOT / "demo"))
    muse = FakeMuse({"ads": [
        {"id": LACE, "note": "4 of 5 asked for a lower price. Put ₦14,000 a yard on the picture.", "picture": "wine lace on a stand"},
        {"id": ANKARA, "note": "Raise the budget to ₦50,000 and you'll sell more.", "picture": ""},  # invented amount
        {"id": ASO, "note": "2 of 6 people bought. Keep it running.", "picture": ""},  # wrong count: it had 2 chats
    ]})
    notes = run(ads_mod.advise(d, rep, muse))
    assert "last price" in muse.seen.lower() and "<numbers>" in muse.seen  # Muse sees what customers wrote
    assert notes[LACE] == {"note": "4 of 5 asked for a lower price. Put ₦14,000 a yard on the picture.",
                           "picture": "wine lace on a stand", "by": "muse-spark-1.3"}
    assert notes[ANKARA]["by"] == "rules" and "₦50,000" not in notes[ANKARA]["note"]
    assert notes[ASO]["by"] == "rules"
    assert run(ads_mod.advise(d, rep, muse)) == notes and muse.calls == 1  # same numbers: no second call
    card = views.ads_card(rep, notes, live=False)
    lace = next(r for r in card["rows"] if r["id"] == LACE)
    assert lace["by_muse"] and lace["note"].startswith("4 of 5")


def test_rule_notes_without_a_key(make_desk):
    d = make_desk()
    seed(d)
    rep = ads_mod.report(d, ads_mod.sample_ads(ROOT / "demo"))
    notes = run(ads_mod.advise(d, rep, None))
    assert "4 of 5 people asked for a lower price" in notes[LACE]["note"]
    assert notes[ASO]["note"].startswith("Your best ad")


class FakeSenses:
    ready = True

    def __init__(self, heard="", receipt=None):
        self.heard, self.receipt = heard, receipt

    async def hear(self, audio, keywords=None):
        return self.heard

    async def read_receipt(self, image):
        return self.receipt


def test_voice_note_is_answered_as_text(make_desk):
    d = make_desk()
    d.senses = FakeSenses(heard="Abeg how much be the blue ankara")
    out = run(d.handle_media("whatsapp", "234802", "audio", b"OggS..."))
    assert "₦6,000" in out.replies[0].text
    assert d.store.history(d.store.customer("whatsapp", "234802", d.now())["id"])[0]["text"].startswith("(voice note) Abeg")


def test_receipt_photo_helps_the_owner_but_never_confirms(make_desk):
    d = make_desk()
    run(d.handle("whatsapp", "234803", "6 yards of the blue ankara, Independence Layout"))
    run(d.handle("whatsapp", "234803", "yes"))
    d.senses = FakeSenses(receipt=Receipt(30000, "ADA OKAFOR", "NF-1", "Opay", ""))
    out = run(d.handle_media("whatsapp", "234803", "image", b"\xff\xd8jpeg"))
    order = d.store.open_order(d.store.customer("whatsapp", "234803", d.now())["id"])
    assert order["stage"] == "awaiting_payment" and order["says_paid"] == 1  # still waiting for real money
    note = out.notes[-1].text
    assert "Careful: it shows ₦30,000, but NF-1 is ₦38,500" in note and "order code is in the description" in note
    assert "isn't proof" in note


def test_photo_without_a_key_is_a_placeholder(make_desk):
    d = make_desk()
    out = run(d.handle_media("whatsapp", "234804", "image", b"\xff\xd8jpeg"))
    assert out.replies  # the robot still answers ("[sent a image]")


def test_muse_brain_request_and_answer():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["auth"], seen["body"] = request.url.path, request.headers["Authorization"], json.loads(request.content)
        answer = {"reply": "We have it.", "intent": "price", "memory": {}, "action": "none", "items": [], "zone": "",
                  "handoff_reason": "", "sources": ["P:ANK-BLU"], "confidence": "high"}
        return httpx.Response(200, json={"model": "muse-spark-1.3", "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]})

    client = MetaClient("test-key", transport=httpx.MockTransport(handler))
    turn = run(MuseBrain(Settings(provider="muse"), client).think("system", "context"))
    assert turn.data["reply"] == "We have it." and turn.model == "muse-spark-1.3"
    assert seen["path"] == "/v1/chat/completions" and seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "muse-spark-1.3" and seen["body"]["response_format"]["type"] == "json_schema"


def test_marketing_api_spend_is_summed_per_ad():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v23.0/act_998877/insights"
        assert request.url.params["level"] == "ad" and request.url.params["date_preset"] == "yesterday"
        return httpx.Response(200, json={"data": [
            {"ad_id": LACE, "ad_name": "Lace for owambe", "spend": "4000.40", "account_currency": "NGN", "publisher_platform": "facebook"},
            {"ad_id": LACE, "ad_name": "Lace for owambe", "spend": "1999.70", "account_currency": "NGN", "publisher_platform": "instagram"},
        ]})

    s = Settings(meta_ads_token="t", meta_ad_account="998877")
    got = run(ads_mod.fetch_ads(s, transport=httpx.MockTransport(handler)))
    assert [(a.id, a.spend, a.platform) for a in got] == [(LACE, 6000, "facebook")]
