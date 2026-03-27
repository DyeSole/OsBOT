"""MiniMax Text-to-Speech client (T2A v2)."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

DEFAULT_MODEL = "speech-02-hd"
API_URL = "https://api.minimax.io/v1/t2a_v2"


def synthesize(
    text: str,
    *,
    api_key: str,
    voice_id: str,
    speed: float = 1.0,
    pitch: int = 0,
    emotion: str = "",
) -> bytes | None:
    """Synthesize *text* to mp3 bytes. Returns None on failure."""
    if not api_key or not voice_id or not text.strip():
        return None

    voice_setting: dict = {
        "voice_id": voice_id,
        "speed": max(0.5, min(2.0, speed)),
        "vol": 1.0,
        "pitch": max(-12, min(12, pitch)),
    }
    if emotion:
        voice_setting["emotion"] = emotion

    payload = {
        "model": DEFAULT_MODEL,
        "text": text[:10000],
        "stream": False,
        "output_format": "hex",
        "voice_setting": voice_setting,
        "audio_setting": {
            "format": "mp3",
            "sample_rate": 32000,
            "bitrate": 128000,
            "channel": 1,
        },
    }

    try:
        resp = httpx.post(
            API_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        hex_audio = data.get("data", {}).get("audio", "")
        if not hex_audio:
            log.warning("tts: empty audio in response: %s", str(data)[:500])
            return None
        return bytes.fromhex(hex_audio)
    except Exception:
        log.exception("tts: synthesize failed")
        return None
