"""Shelf camera (SAM 3.1 counts) and Ask Sales Desk. No network: SAM's reply and Muse's answers are faked."""

import asyncio
import json

import httpx

from salesdesk import ask
from salesdesk.meta import MetaClient
from salesdesk.seed import seed
from salesdesk.shelf import ShelfCam, keep


def run(coro):
    return asyncio.run(coro)


def sam_reply(boxes):
    text = "".join(f"<|box;x1={a};y1={b};x2={c};y2={d};w=1600;h=1600|><|mask;x=0;y=0;data=1,2,~|>" for a, b, c, d in boxes)
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}


def test_segment_reads_sam_boxes():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=sam_reply([(100, 200, 500, 600), (0, 1200, 150, 1599)]))

    c = MetaClient("k", transport=httpx.MockTransport(handler))
    boxes = run(c.segment(b"\x89PNG\r\n\x1a\nxx", "stack of folded fabric"))
    assert seen["model"] == "sam-3.1"
    assert seen["input"][0]["content"][0] == {"type": "input_text", "text": "stack of folded fabric"}
    assert seen["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert boxes[0] == (0.0625, 0.125, 0.3125, 0.375)
    assert keep(boxes) == [boxes[0]]  # the second one is cut off by the frame edge


def test_keep_drops_specks():
    assert keep([(0.1, 0.1, 0.12, 0.12), (0.2, 0.2, 0.5, 0.5)]) == [(0.2, 0.2, 0.5, 0.5)]


class FakeSam:
    def __init__(self, *counts):
        self.counts = list(counts)

    async def segment(self, image, phrase):
        n = self.counts.pop(0)
        return [(0.05 + i * 0.1, 0.2, 0.12 + i * 0.1, 0.4) for i in range(n)]


def test_shelf_count_drop_without_a_sale(make_desk):
    d = make_desk()
    cam = ShelfCam()
    sam = FakeSam(9, 7)
    run(cam.snap(sam, b"x", d.now(), "/a.jpg"))
    first = cam.report(d)
    assert first["latest"]["count"] == 9 and first["gap"] is None
    run(cam.snap(sam, b"x", d.now(), "/b.jpg"))
    r = cam.report(d)
    assert r["latest"]["count"] == 7 and r["log"][-1]["taken"] == 2
    assert r["gap"]["taken"] == 2 and r["gap"]["unsold"] == 2 - r["gap"]["sold"]


class FakeMuse:
    def __init__(self, answer):
        self.answer = answer

    async def chat_json(self, system, text, schema, name, **kw):
        self.text = text
        return {"answer": self.answer}, "muse-spark-1.3"


def test_ask_answers_from_the_brief(make_desk):
    d = make_desk()
    seed(d)
    rep = {"rows": [], "spend": 0, "back": 0, "chats": 0, "paid": 0}
    muse = FakeMuse("You sold well since yesterday, ma.")
    r = run(ask.answer(d, rep, muse, "How did I do?"))
    assert r["by"] == "muse-spark-1.3" and r["answer"].startswith("You sold")
    assert "<brief>" in muse.text and "<question>How did I do?</question>" in muse.text


def test_ask_drops_an_invented_amount(make_desk):
    d = make_desk()
    seed(d)
    rep = {"rows": [], "spend": 0, "back": 0, "chats": 0, "paid": 0}
    r = run(ask.answer(d, rep, FakeMuse("You made ₦999,999 today."), "How much?"))
    assert r["by"] == "brief" and "999,999" not in r["answer"]


def test_ask_without_a_key_reads_the_headline(make_desk):
    d = make_desk()
    r = run(ask.answer(d, {"rows": []}, None, "Anything new?"))
    assert r["by"] == "brief" and r["answer"]
