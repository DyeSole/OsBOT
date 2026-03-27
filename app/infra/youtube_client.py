from __future__ import annotations

import re
from urllib.parse import urlparse, parse_qs

_YOUTUBE_DOMAINS = {"youtube.com", "youtu.be"}
_VIDEO_ID_PATTERN = re.compile(r"(?:v=|youtu\.be/|/embed/|/v/)([a-zA-Z0-9_-]{11})")


def is_youtube_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in _YOUTUBE_DOMAINS)


def extract_video_id(url: str) -> str | None:
    m = _VIDEO_ID_PATTERN.search(url)
    return m.group(1) if m else None


async def fetch_youtube_transcript(url: str) -> str | None:
    """Fetch YouTube video transcript. Returns formatted text or None."""
    import asyncio

    video_id = extract_video_id(url)
    if not video_id:
        return None

    def _get_transcript():
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, CouldNotRetrieveTranscript
        api = YouTubeTranscriptApi()
        try:
            # Try preferred languages first
            for langs in (["zh-Hans", "zh-Hant", "zh"], ["en"]):
                try:
                    return api.fetch(video_id, languages=langs)
                except Exception:
                    pass
            # Fall back: list and take first available
            tl = api.list(video_id)
            first = next(iter(tl), None)
            if first:
                return api.fetch(video_id, languages=[first.language_code])
        except (NoTranscriptFound, CouldNotRetrieveTranscript):
            return None
        except Exception:
            return None

    snippets = await asyncio.to_thread(_get_transcript)
    if not snippets:
        return None

    lines = [s.text.strip() for s in snippets if s.text.strip()]
    text = " ".join(lines)
    return text[:3000] if text else None


async def fetch_youtube_info(url: str) -> str | None:
    """Fetch YouTube video title + transcript."""
    import httpx

    video_id = extract_video_id(url)
    if not video_id:
        return None

    parts: list[str] = []

    # Try to get title from oEmbed (no API key needed)
    try:
        import urllib.parse
        encoded = urllib.parse.quote(url, safe="")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://www.youtube.com/oembed?url={encoded}&format=json")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("title"):
                    parts.append(f"标题: {data['title']}")
                if data.get("author_name"):
                    parts.append(f"频道: {data['author_name']}")
    except Exception:
        pass

    transcript = await fetch_youtube_transcript(url)
    if transcript:
        parts.append(f"字幕内容:\n{transcript}")
    else:
        parts.append("[无字幕]")

    return "\n".join(parts) if parts else None
