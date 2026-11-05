"""Meta Model API (https://dev.meta.ai/docs): Muse Spark for chat and images in, Muse Voice Transcribe for voice
notes, Muse Image for ad pictures, SAM 3.1 for counting stock on a shelf. Plain httpx, no SDK.

Every call here only reads or drafts. Nothing in this file sends a message, changes an order or spends money."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess

import httpx

BASE_URL = "https://api.meta.ai/v1"
SAM_BOX = re.compile(r"<\|box;x1=(\d+);y1=(\d+);x2=(\d+);y2=(\d+);w=(\d+);h=(\d+)\|>")


class MetaError(Exception):
    """A Meta Model API call failed."""


class MetaClient:
    def __init__(self, api_key: str, model: str = "muse-spark-1.3", base_url: str = BASE_URL,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise MetaError("no Meta Model API key (set META_API_KEY)")
        self.api_key, self.model, self.base_url = api_key, model, base_url
        self.transport = transport

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=self.transport,
                                 headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"})

    async def _post(self, path: str, timeout: float = 60, **kwargs) -> dict:
        try:
            async with self._client(timeout) as c:
                r = await c.post(path, **kwargs)
        except httpx.HTTPError as exc:
            raise MetaError(f"connection error: {exc}") from exc
        if r.status_code >= 300:
            raise MetaError(f"Meta API {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError as exc:
            raise MetaError("Meta API sent something that isn't JSON") from exc

    async def chat_json(self, system: str, text: str, schema: dict, name: str, image: bytes | None = None,
                        effort: str = "low", max_tokens: int = 1500) -> tuple[dict, str]:
        """Muse Spark with a JSON schema, optionally looking at one picture. Returns (parsed answer, model id)."""
        content: list[dict] = [{"type": "text", "text": text}]
        if image:
            b64 = base64.b64encode(image).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:{image_mime(image)};base64,{b64}"}})
        body = {
            "model": self.model, "reasoning_effort": effort, "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema}},
        }
        reply = await self._post("/chat/completions", json=body)
        try:
            choice = reply["choices"][0]
            if choice.get("finish_reason") == "length":
                raise MetaError("Muse ran out of room before answering")
            return json.loads(choice["message"]["content"]), reply.get("model") or self.model
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise MetaError("unreadable Muse Spark answer") from exc

    async def transcribe(self, wav: bytes, keywords: list[str] | None = None) -> str:
        """Muse Voice Transcribe. Takes 16 kHz mono WAV (see to_wav)."""
        settings = {"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}
        if keywords:
            settings["keywords"] = keywords[:50]
        files = {"request": (None, json.dumps(settings), "application/json"), "audio": ("note.wav", wav, "audio/wav")}
        reply = await self._post("/asr/transcribe", timeout=90, files=files)
        return str(reply.get("transcript") or "").strip()

    async def image(self, prompt: str, size: str = "1024x1024") -> bytes:
        """Muse Image: one picture, returned as bytes (webp by default)."""
        reply = await self._post("/images/generations", timeout=120, json={
            "model": "muse-image-1.0", "prompt": prompt, "n": 1, "size": size, "response_format": "b64_json"})
        try:
            return base64.b64decode(reply["data"][0]["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MetaError("unreadable Muse Image answer") from exc


    async def segment(self, image: bytes, phrase: str) -> list[tuple[float, float, float, float]]:
        """SAM 3.1: every match for a short noun phrase, as boxes (x1, y1, x2, y2) in 0-1 of the picture."""
        b64 = base64.b64encode(image).decode()
        reply = await self._post("/responses", timeout=120, json={"model": "sam-3.1", "input": [{
            "type": "message", "role": "user", "content": [
                {"type": "input_text", "text": phrase},
                {"type": "input_image", "image_url": f"data:{image_mime(image)};base64,{b64}"}]}]})
        try:
            text = "".join(c.get("text", "") for o in reply["output"] for c in o.get("content", []))
        except (KeyError, TypeError) as exc:
            raise MetaError("unreadable SAM 3.1 answer") from exc
        return [(x1 / w, y1 / h, x2 / w, y2 / h)
                for x1, y1, x2, y2, w, h in ((int(v) for v in m) for m in SAM_BOX.findall(text)) if w and h]


def image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def to_wav(audio: bytes) -> bytes:
    """Any voice note (WhatsApp OGG/Opus, browser WebM, MP3, M4A, WAV at any rate) to 16 kHz mono 16-bit WAV, the
    only format Muse Voice Transcribe accepts. Needs ffmpeg on the PATH."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        if audio[:4] == b"RIFF":
            return audio  # may still be refused if it isn't 16 or 24 kHz mono
        raise MetaError("ffmpeg isn't installed, so voice notes can't be converted")
    p = subprocess.run([ffmpeg, "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                        "-f", "wav", "pipe:1"],
                       input=audio, capture_output=True, timeout=60)
    if p.returncode != 0 or not p.stdout:
        raise MetaError(f"couldn't convert the voice note: {p.stderr.decode(errors='ignore')[:200]}")
    return p.stdout
