from __future__ import annotations

import io
import re

DEFAULT_EDGE_VOICE = "zh-CN-XiaoyiNeural"
DEFAULT_MINIMAX_VOICE = "female-shaonv"
MINIMAX_DEFAULT_URL = "https://api.minimax.chat/v1/t2a_v2"


def _clean_for_tts(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`~|#>]", "", text)
    # Strip prosody markers unsupported by speech-02-hd (e.g. [0.5s], (breath), (laughter))
    text = re.sub(r"\[\d+(?:\.\d+)?s\]", "", text)
    text = re.sub(r"\((?:breath|laughter|sigh|cough)\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]


async def _synthesize_minimax(text: str, voice: str, api_key: str, base_url: str, *, model: str = "speech-02-hd", speed: float = 1.0, emotion: str = "") -> bytes | None:
    import requests
    url = base_url.rstrip("/") if base_url else MINIMAX_DEFAULT_URL
    payload = {
        "model": model,
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": voice or DEFAULT_MINIMAX_VOICE,
            "speed": speed,
            "vol": 1.0,
            "pitch": 0,
            **( {"emotion": emotion} if emotion else {} ),
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
        },
    }
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        hex_audio = data.get("data", {}).get("audio", "")
        if not hex_audio:
            return None
        return bytes.fromhex(hex_audio)
    except Exception:
        return None


async def _synthesize_edge(text: str, voice: str) -> bytes | None:
    try:
        import edge_tts
        communicate = edge_tts.Communicate(text, voice or DEFAULT_EDGE_VOICE)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        data = buf.getvalue()
        return data if data else None
    except Exception:
        return None


async def synthesize(text: str, voice: str = "", *, api_key: str = "", base_url: str = "", model: str = "speech-02-hd", speed: float = 1.0, emotion: str = "") -> bytes | None:
    cleaned = _clean_for_tts(text)
    if not cleaned:
        return None
    if api_key:
        result = await _synthesize_minimax(cleaned, voice or DEFAULT_MINIMAX_VOICE, api_key, base_url, model=model, speed=speed, emotion=emotion)
        if result:
            return result
    return await _synthesize_edge(cleaned, voice or DEFAULT_EDGE_VOICE)
