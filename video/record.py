"""Record the Sales Desk demo video (SCRIPT.md) against the running app, with narration.

    python video/record.py --base http://127.0.0.1:8791 --rehearse   # walk the story, no video, prints replies
    python video/record.py --base http://127.0.0.1:8791              # record video/out/sales-desk-demo.mp4

The app must run with a real META_API_KEY (Muse answers live). The demo is reset at the start.
Voices are neural voices through edge-tts (online): a US narrator and a Nigerian customer for the voice note.
Edge is driven by Playwright with a fake microphone that plays the customer's voice note, so the real
"Record a voice note" button and Muse Voice Transcribe are used. Long waits for the models are cut out at the
end (speech is never cut). Adapted from demo-video/record_demo.py (DocDesk).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
CACHE = HERE / "voice-cache"
W, H = 1280, 720
NARRATOR, CUSTOMER = "en-US-AndrewNeural", "en-NG-EzinneNeural"
ANKARA, LACE = "120211000000000101", "120211000000000102"
MAX_GAP = 2.2  # silent stretches longer than this are shortened

# Spoken text (numbers written the way they should be said). Captions and cards carry the ₦ figures.
NARRATION = {
    "intro": "Nkem sells fabric in Enugu. Yesterday she paid Meta twelve thousand five hundred naira for ads. "
             "Which ad made her money? She can't tell.",
    "problem": "Her ads open WhatsApp chats. Some people buy. Many ask for last price, and disappear. "
               "Sales Desk answers every chat, and remembers which ad sent each customer.",
    "ad": "A customer taps the Ankara ad. WhatsApp tells Sales Desk which ad sent her, and Muse Spark answers "
          "from the real price list.",
    "voice": "She sends a voice note, like most customers here do.",
    "voice_after": "Muse Voice Transcribe turns it into text. The total is exact, because prices come from the "
                   "price list, never from the model.",
    "yes": "She says yes and gets an order code. Then she sends a screenshot.",
    "receipt": "Muse Spark reads the amount and the code. Screenshots are easy to fake, so Sales Desk never "
               "confirms one.",
    "alert": "Her bank's signed alert email confirms it, and she hears back in seconds.",
    "brief": "Every morning, Nkem opens her brief. What each ad cost sits next to the money it really brought in. "
             "Aso oke paid back thirty five naira for every naira. The lace ad spent six thousand naira, and sold nothing.",
    "muse": "Muse Spark read what those customers wrote. Four of five asked for last price. So it suggests one "
            "thing to try.",
    "image": "Then Muse Image drafts a new lace photo. Sales Desk prints the real price on it, because image models "
             "garble text. Nothing gets posted. Nkem decides.",
    "outro": "Sales Desk. Every chat answered, and you know which ad made money. Built during this hackathon on "
             "Muse Spark, Muse Voice Transcribe and Muse Image. The shop and ad spend here are samples. "
             "Connecting real WhatsApp numbers and ad accounts comes next.",
}
VOICE_NOTE = "Six yards of the blue Ankara. Deliver to Independence Layout."

events: list[tuple[float, Path]] = []  # (seconds from video start, wav)
t0 = 0.0
audio_end = 0.0
REHEARSE = False


# ------------------------------------------------------------------ audio
def tts(text: str, voice: str, rate: int = 0) -> tuple[Path, float]:
    """One line in a neural voice (edge-tts), cached as 48 kHz mono WAV. Returns (wav, seconds)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{voice}|{rate}|{text}".encode()).hexdigest()[:16]
    mp3, wav = CACHE / f"{key}.mp3", CACHE / f"{key}.wav"
    if not wav.exists():
        r = None
        for _ in range(5):  # the voice service is online; retry a slow or dropped request
            try:
                r = subprocess.run([sys.executable, "-m", "edge_tts", "--voice", voice, f"--rate={rate:+d}%",
                                    "--text", text, "--write-media", str(mp3)], capture_output=True, timeout=60)
            except subprocess.TimeoutExpired:
                continue
            if r.returncode == 0 and mp3.exists() and mp3.stat().st_size > 1000:
                break
            time.sleep(1.5)
        else:
            raise RuntimeError(f"voice synthesis failed: {r.stderr.decode(errors='replace')[-300:] if r else 'timeout'}")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp3), "-ar", "48000", "-ac", "1", str(wav)],
                       check=True, capture_output=True)
    return wav, wav_len(wav)


def wav_len(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def mic_file(clip: Path) -> Path:
    """The fake microphone's input: the voice note with a little silence first, 16-bit PCM."""
    out = CACHE / (clip.stem + "-mic.wav")
    if not out.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(clip), "-af", "adelay=300|300,apad=pad_dur=1",
                        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(out)], check=True, capture_output=True)
    return out


def now() -> float:
    return time.monotonic() - t0


def play(clip: tuple[Path, float], wait: bool = True, gap: float = 0.35) -> None:
    """Schedule a clip now (never over the previous one). With wait, sleep until it has finished."""
    global audio_end
    at = max(now(), audio_end + 0.25)
    events.append((at, clip[0]))
    audio_end = at + clip[1]
    if wait and not REHEARSE:
        time.sleep(max(0.0, at + clip[1] + gap - now()))


def until_quiet(extra: float = 0.3) -> None:
    if not REHEARSE:
        time.sleep(max(0.0, audio_end + extra - now()))


# ------------------------------------------------------------------ page helpers
OVERLAY_JS = """
() => {
  if (!document.getElementById('demo-cursor')) {
    const c = document.createElement('div');
    c.id = 'demo-cursor';
    c.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24"><path d="M5 3L19 12L12 13L9 20L5 3Z" fill="white" stroke="black" stroke-width="1.5" stroke-linejoin="round"/></svg>';
    c.style.cssText = 'position:fixed;z-index:999999;pointer-events:none;width:24px;height:24px;left:640px;top:360px;transition:left .1s,top .1s;filter:drop-shadow(1px 1px 2px rgba(0,0,0,.3))';
    document.body.appendChild(c);
    document.addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; });
  }
  if (!document.getElementById('demo-subtitle')) {
    const b = document.createElement('div');
    b.id = 'demo-subtitle';
    b.style.cssText = 'position:fixed;left:50%;transform:translateX(-50%);bottom:16px;z-index:999998;max-width:80%;text-align:center;padding:9px 18px;border-radius:12px;background:rgba(22,24,29,.88);color:#fff;font:600 17px/1.35 Inter,"Segoe UI",sans-serif;opacity:0;transition:opacity .3s;pointer-events:none';
    document.body.appendChild(b);
  }
}
"""


def overlays(page: Page) -> None:
    page.evaluate(OVERLAY_JS)


def caption(page: Page, text: str) -> None:
    page.evaluate("t => { const b = document.getElementById('demo-subtitle'); if (!b) return;"
                  " if (t) { b.textContent = t; b.style.opacity = '1'; } else { b.style.opacity = '0'; } }", text)


def card(page: Page, title: str, lines: list[str], show: bool = True) -> None:
    """Full-screen title or closing card, in the app's own colours."""
    page.evaluate("""([title, lines, show]) => {
      let el = document.getElementById('demo-card');
      if (!show) { if (el) { el.style.opacity = '0'; setTimeout(() => el.remove(), 600); } return; }
      if (el) el.remove();
      el = document.createElement('div');
      el.id = 'demo-card';
      el.style.cssText = 'position:fixed;inset:0;z-index:999997;display:flex;flex-direction:column;justify-content:center;gap:14px;padding:0 110px;background:#16181D;color:#fff;font-family:Inter,"Segoe UI",sans-serif;transition:opacity .6s';
      el.innerHTML = '<div style="font:700 64px/1 \\'Bricolage Grotesque\\',Inter,sans-serif;letter-spacing:-1px">' + title + '</div>'
        + lines.map((l, i) => '<div style="font-size:' + (i ? 20 : 30) + 'px;color:' + (i ? '#B9BDC7' : '#7FD8A8') + ';' + (i ? '' : 'margin-bottom:10px;font-weight:600') + '">' + l + '</div>').join('');
      document.body.appendChild(el);
    }""", [title, lines, show])


def move_to(page: Page, selector: str, steps: int = 14) -> None:
    el = page.locator(selector).first
    el.scroll_into_view_if_needed()
    box = el.bounding_box()
    if box:
        page.mouse.move(box["x"] + min(box["width"] / 2, 180), box["y"] + box["height"] / 2, steps=steps)
        time.sleep(0.4)


def click(page: Page, selector: str, pause: float = 0.4) -> None:
    move_to(page, selector)
    page.locator(selector).first.click()
    time.sleep(pause)


def type_in(page: Page, selector: str, text: str, delay: int = 38) -> None:
    click(page, selector, pause=0.2)
    page.locator(selector).first.fill("")
    page.locator(selector).first.press_sequentially(text, delay=delay)
    time.sleep(0.3)


def shop_count(page: Page) -> int:
    return page.locator("#msgs .msg.shop").count()


def wait_reply(page: Page, before: int, timeout: int = 120000) -> str:
    page.wait_for_function(f"document.querySelectorAll('#msgs .msg.shop').length > {before}", timeout=timeout, polling=250)
    time.sleep(0.6)
    said = page.locator("#msgs .msg.shop .bub").last.inner_text()
    print("  robot:", said[:140])
    return said


def send(page: Page, text: str) -> str:
    before = shop_count(page)
    type_in(page, "#text", text)
    page.keyboard.press("Enter")
    return wait_reply(page, before)


def post(base: str, path: str, timeout: int = 180) -> dict:
    req = urllib.request.Request(base + path, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


# ------------------------------------------------------------------ the story
def story(page: Page, base: str, narr: dict, voice_note: tuple[Path, float]) -> None:
    page.goto(base + "/phone")
    page.wait_for_selector("#ad option[value='%s']" % ANKARA, state="attached", timeout=20000)
    overlays(page)
    card(page, "Sales Desk", ["Know which ad made money.",
                              "Muse Spark · Muse Voice Transcribe · Muse Image",
                              "Built 4 Nov 2026 for the Meta Global AI Developer Hackathon · demo shop: made up"])
    time.sleep(0.8)
    play(narr["intro"], gap=0.2)
    card(page, "", [], show=False)
    caption(page, "Sales Desk · which ad made money")
    play(narr["problem"], wait=False)
    move_to(page, "#ad")
    page.select_option("#ad", ANKARA)
    time.sleep(0.8)
    until_quiet(0.1)

    caption(page, "1. A customer taps the Ankara ad")
    play(narr["ad"], wait=False)
    send(page, "Good evening! I saw your Ankara ad. Do you have the blue and gold one?")
    until_quiet(0.6)

    caption(page, "2. Voice notes, answered")
    play(narr["voice"])
    before = shop_count(page)
    click(page, "#voice", pause=0.1)
    play(voice_note, wait=False)  # the same recording the fake microphone is playing
    time.sleep(voice_note[1] + 0.8)
    click(page, "#voice", pause=0.2)
    wait_reply(page, before)
    play(narr["voice_after"])

    caption(page, "3. A receipt is read, never trusted")
    play(narr["yes"], wait=False)
    said = send(page, "Yes, hold it for me")
    code = re.search(r"\b([A-Z]{1,3}-\d+)\b", said)
    total = re.search(r"₦([\d,]+)", said)
    if not code or not total:
        raise RuntimeError(f"no order code or total in: {said}")
    type_in(page, "#amount", total.group(1).replace(",", ""), delay=25)
    type_in(page, "#sender", "CHIDINMA OBI", delay=25)
    type_in(page, "#narration", code.group(1), delay=25)
    until_quiet(0.1)
    before = shop_count(page)
    click(page, "#asReceipt", pause=0.3)
    wait_reply(page, before)
    page.wait_for_selector("#ownerPanel:not([hidden])", timeout=30000)
    move_to(page, "#ownerNotes")
    play(narr["receipt"])

    caption(page, "Confirmed only by the bank's signed alert")
    play(narr["alert"], wait=False)
    before = shop_count(page)
    click(page, "#sendAlert", pause=0.3)
    wait_reply(page, before)
    move_to(page, "#msgs .msg.shop:last-child")
    until_quiet(0.8)

    notes = post(base, "/api/ads/notes")  # warm Muse's ad notes (the wait is cut from the video)
    print("  ad notes ready:", [r["name"] + ": " + r["note"][:60] for r in notes.get("rows", [])])
    page.goto(base + "/")
    page.wait_for_selector("#ads:not([hidden]) .adt-row", timeout=30000)
    page.wait_for_function("!/Reading what/.test(document.getElementById('adMuse').innerText)", timeout=120000)
    overlays(page)
    page.add_style_tag(content="#ads { zoom: 1.12; } #adDraft img { width: 300px; }")  # easier to read at 720p
    caption(page, "4. Every morning: which ad made money")
    page.evaluate("document.getElementById('ads').scrollIntoView({block: 'start', behavior: 'smooth'})")
    time.sleep(1.0)
    play(narr["brief"], wait=False)
    move_to(page, "#adSpend")
    time.sleep(2.5)
    move_to(page, ".adt-row:not(.losing) .chip.best")
    time.sleep(2.0)
    move_to(page, ".adt-row.losing")
    until_quiet(0.2)

    caption(page, "Muse Spark reads what customers wrote")
    move_to(page, "#adMuse")
    play(narr["muse"])

    caption(page, "5. Muse Image drafts a better ad")
    click(page, "[data-act=draft]", pause=0.3)
    page.wait_for_selector("#adDraft img", timeout=180000)  # the wait is silent, so the edit cuts it
    page.wait_for_function("document.querySelector('#adDraft img').naturalWidth > 0", timeout=30000)
    page.evaluate("document.querySelector('#adDraft img').scrollIntoView({block: 'center', behavior: 'smooth'})")
    time.sleep(0.6)
    play(narr["image"], wait=False)  # speak over the finished picture, never over the wait
    move_to(page, "#adDraft img")
    until_quiet(1.0)

    caption(page, "")
    card(page, "Sales Desk", ["Know which ad made money.",
                              "Muse Spark 1.3: chats, receipts, ad notes · Muse Voice Transcribe: voice notes · Muse Image: ad drafts",
                              "WhatsApp and Instagram ad referrals · Marketing API spend, read-only",
                              "Built during the hackathon, 4 Nov 2026 · github.com/Okorodavid/sales-desk",
                              "Demo shop and ad spend are samples. WhatsApp, Instagram and Marketing API connections are coded, not run live yet."])
    time.sleep(0.6)
    play(narr["outro"], gap=1.5)


# ------------------------------------------------------------------ edit
def build(raw: Path, out: Path) -> float:
    """Mix the speech onto the recording, shortening silent stretches longer than MAX_GAP."""
    clips = sorted(((at, at + wav_len(p), str(p)) for at, p in events), key=lambda c: c[0])
    end = max(c[1] for c in clips) + 1.2
    cuts, covered = [], clips[0][1]
    for start, stop, _ in clips[1:]:
        if start - covered > MAX_GAP:
            cuts.append((covered + MAX_GAP / 2, start - MAX_GAP / 2))
        covered = max(covered, stop)
    keep, t = [], 0.0
    for a, b in cuts:
        keep.append((t, a))
        t = b
    keep.append((t, end))

    def shift(at: float) -> float:
        return at - sum(b - a for a, b in cuts if b <= at)

    new_end = sum(b - a for a, b in keep)
    select = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keep)
    inputs, filters = ["-i", str(raw)], [f"[0:v]fps=30,select='{select}',setpts=N/30/TB[vout]"]
    for i, (start, _, path) in enumerate(clips, start=1):
        inputs += ["-i", path]
        ms = int(shift(start) * 1000)
        filters.append(f"[{i}:a]aresample=48000,adelay={ms}|{ms}[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(1, len(clips) + 1))
    filters.append(f"{mix}amix=inputs={len(clips)}:normalize=0,volume=1.1,alimiter=limit=0.89:level=false[aout]")
    subprocess.run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
                    "-t", f"{new_end:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-r", "30", "-c:a", "aac", "-b:a", "160k", str(out)], check=True, capture_output=True)
    print(f"cut {len(cuts)} silent stretches ({end - new_end:.1f}s)")
    return new_end


def main() -> int:
    global t0, REHEARSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8791")
    ap.add_argument("--rehearse", action="store_true")
    args = ap.parse_args()
    REHEARSE = args.rehearse
    base = args.base.rstrip("/")

    print("voices...")
    narr = {k: tts(v, NARRATOR) for k, v in NARRATION.items()}
    voice_note = tts(VOICE_NOTE, CUSTOMER, rate=-4)
    print(f"narration {sum(d for _, d in narr.values()):.0f}s, voice note {voice_note[1]:.1f}s")

    post(base, "/api/demo/reset")
    raw_dir = OUT / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={mic_file(voice_note[0])}", "--autoplay-policy=no-user-gesture-required"])
        opts = {"viewport": {"width": W, "height": H}, "color_scheme": "light", "permissions": ["microphone"]}
        if not REHEARSE:
            opts.update(record_video_dir=str(raw_dir), record_video_size={"width": W, "height": H})
        context = browser.new_context(**opts)
        page = context.new_page()
        t0 = time.monotonic()
        try:
            story(page, base, narr, voice_note)
            print(f"story finished at {now():.1f}s")
        finally:
            video = page.video
            context.close()
            raw = Path(video.path()) if video else None
            browser.close()
    if REHEARSE:
        print("REHEARSAL PASSED")
        return 0
    (OUT / "timeline.json").write_text(json.dumps([{"at": round(a, 2), "clip": str(c)} for a, c in events], indent=2))
    out = OUT / "sales-desk-demo.mp4"
    length = build(raw, out)
    print(f"wrote {out} ({length:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
