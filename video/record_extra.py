"""Silent screen clips for the founder-narrated cut: the brief, the order board, Ask Sales Desk by voice and the
shelf camera. Each clip is a 1280x720 WebM plus a list of moments (seconds from the clip start), so the edit
can line the founder's words up with what happens on screen and cut the model waits.

    python video/record_extra.py --base http://127.0.0.1:8791

Uses the helpers in record.py. The app must run with a real META_API_KEY. The demo is reset first.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import record as R

OUT = R.OUT / "extra"
QUESTION = "Which of my ads is losing money?"
OWNER_VOICE = "en-NG-EzinneNeural"


def clip(browser, name: str, fn, mic: Path | None = None) -> None:
    opts = {"viewport": {"width": R.W, "height": R.H}, "color_scheme": "light", "permissions": ["microphone"],
            "record_video_dir": str(OUT / "tmp"), "record_video_size": {"width": R.W, "height": R.H}}
    context = browser.new_context(**opts)
    page = context.new_page()
    t0 = time.monotonic()
    marks: dict[str, float] = {}
    mark = lambda key: marks.__setitem__(key, round(time.monotonic() - t0, 2))  # noqa: E731
    fn(page, mark)
    mark("end")
    video = page.video
    context.close()
    shutil.move(video.path(), OUT / f"{name}.webm")
    (OUT / f"{name}.json").write_text(json.dumps(marks, indent=2))
    print(name, marks)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8791")
    ap.add_argument("--only", default="")
    base = ap.parse_args().base.rstrip("/")
    OUT.mkdir(parents=True, exist_ok=True)
    question = R.tts(QUESTION, OWNER_VOICE, rate=-4)
    R.post(base, "/api/demo/reset")

    def brief(page, mark):
        page.goto(base + "/")
        page.wait_for_selector("#ads:not([hidden]) .adt-row", timeout=30000)
        R.overlays(page)
        time.sleep(1.5)
        mark("top")
        time.sleep(3)
        for y in (330, 900):  # whole-screen steps; a smooth scroll smears in the recording
            page.mouse.wheel(0, y - page.evaluate("window.scrollY"))
            mark(f"at{y}")
            time.sleep(4)

    def board(page, mark):
        page.goto(base + "/board")
        page.wait_for_load_state("networkidle")
        R.overlays(page)
        time.sleep(1.5)
        mark("shown")
        page.mouse.move(300, 300, steps=10)
        for x in (520, 820, 1100):
            page.mouse.move(x, 320, steps=18)
            time.sleep(1.4)
        time.sleep(2)

    def ask(page, mark):
        page.goto(base + "/")
        page.wait_for_selector("#ads:not([hidden]) .adt-row", timeout=30000)
        R.overlays(page)
        time.sleep(1.2)
        R.move_to(page, "#askMic")
        mark("mic")
        page.locator("#askMic").click()
        time.sleep(question[1] + 0.8)  # the fake microphone is saying the question
        page.locator("#askMic").click()
        mark("sent")
        page.wait_for_function("!/Thinking|Listening/.test(document.getElementById('askA').innerText)", timeout=120000)
        mark("answer")
        R.move_to(page, "#askA")
        (OUT / "ask-answer.txt").write_text(page.locator("#askA").inner_text(), encoding="utf-8")
        (OUT / "ask-question.txt").write_text(page.locator("#askQ").inner_text(), encoding="utf-8")
        time.sleep(9)

    def shelf(page, mark):
        page.goto(base + "/shelf")
        R.overlays(page)
        time.sleep(1.2)
        R.move_to(page, "#snap")
        mark("snap1")
        page.locator("#snap").click()
        page.wait_for_selector(".mark", timeout=150000)
        mark("count1")
        time.sleep(3.5)
        R.move_to(page, "#snap2")
        mark("snap2")
        page.locator("#snap2").click()
        page.wait_for_function("document.getElementById('hello').innerText.startsWith('7')", timeout=150000)
        mark("count2")
        time.sleep(2.5)
        R.move_to(page, "#gap")
        time.sleep(5)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={R.mic_file(question[0])}", "--autoplay-policy=no-user-gesture-required"])
        names = sys.argv[sys.argv.index("--only") + 1].split(",") if "--only" in sys.argv else None
        for name, fn in (("brief", brief), ("board", board), ("ask", ask), ("shelf", shelf)):
            if names and name not in names:
                continue
            clip(browser, name, fn)
        browser.close()
    shutil.copy(question[0], OUT / "ask-question.wav")
    return 0


if __name__ == "__main__":
    sys.exit(main())
