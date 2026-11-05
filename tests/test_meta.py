"""The Meta parts: ad referrals, voice notes, receipt photos, and the Muse brain.
No network: Meta's APIs are replaced by httpx mock transports or small fakes."""

import asyncio
import json

import httpx

from salesdesk.brain import MuseBrain
from salesdesk.channels import instagram, whatsapp
from salesdesk.config import Settings
from salesdesk.meta import MetaClient
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
    assert "£12" in out.replies[0].text
    assert d.store.history(d.store.customer("whatsapp", "234802", d.now())["id"])[0]["text"].startswith("(voice note) Abeg")


def test_receipt_photo_helps_the_owner_but_never_confirms(make_desk):
    d = make_desk()
    run(d.handle("whatsapp", "234803", "6 yards of the blue ankara, Peckham"))
    run(d.handle("whatsapp", "234803", "yes"))
    d.senses = FakeSenses(receipt=Receipt(30, "ADA OKAFOR", "ZC-1", "PayPal", ""))
    out = run(d.handle_media("whatsapp", "234803", "image", b"\xff\xd8jpeg"))
    order = d.store.open_order(d.store.customer("whatsapp", "234803", d.now())["id"])
    assert order["stage"] == "approval" and order["says_paid"] == 1  # still waiting: only PayPal can confirm
    note = out.notes[-1].text
    assert "Careful: it shows £30, but ZC-1 is £76" in note
    assert "not proof" in note


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
