from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "browser"
AUTH_DIR = DATA_DIR / "auth"

BILIBILI_DOMAINS = {"bilibili.com", "b23.tv"}
XHS_DOMAINS = {"xiaohongshu.com", "xhslink.com"}
XHS_PROXY = "http://43.159.54.59:9876"

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
YOUTUBE_DOMAINS = {"youtube.com", "youtu.be"}


def _is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in YOUTUBE_DOMAINS)


def _is_bilibili(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in BILIBILI_DOMAINS)


def _is_xhs(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in XHS_DOMAINS)


def extract_urls(text: str) -> list[str]:
    return [u for u in URL_PATTERN.findall(text) if _is_bilibili(u) or _is_xhs(u) or _is_youtube(u)]


async def _resolve_xhs_short_link(url: str) -> str:
    """Resolve xhslink.com short links to full URLs via proxy."""
    import urllib.parse
    import httpx

    if "xhslink.com" not in (urlparse(url).hostname or "").lower():
        return url
    try:
        encoded = urllib.parse.quote(url, safe="")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{XHS_PROXY}/resolve?url={encoded}")
            if resp.status_code == 200:
                return resp.json().get("url") or url
    except Exception:
        pass
    return url


async def _collect_images(data: dict) -> list[bytes]:
    import base64
    import httpx

    images: list[bytes] = []

    img_b64 = data.get("img_b64", "")
    if img_b64:
        try:
            images.append(base64.b64decode(img_b64))
        except Exception:
            pass

    for item in data.get("images", []):
        if not isinstance(item, str):
            continue
        if item.startswith("data:"):
            try:
                images.append(base64.b64decode(item.split(",", 1)[1]))
            except Exception:
                pass
        elif item.startswith("http"):
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(item)
                    if r.status_code == 200:
                        images.append(r.content)
            except Exception:
                pass

    return images


async def fetch_page_content(url: str) -> str | None:
    if _is_bilibili(url):
        from app.infra.bilibili_client import fetch_bilibili
        return await fetch_bilibili(url)
    if _is_xhs(url):
        result = await fetch_xhs_via_proxy(url)
        if result:
            return result[0]
    if _is_youtube(url):
        from app.infra.youtube_client import fetch_youtube_info
        return await fetch_youtube_info(url)
    return None


async def fetch_xhs_via_proxy(url: str) -> tuple[str | None, list[bytes]]:
    """Fetch XHS post content and images via proxy. Returns (text, image_bytes_list)."""
    import urllib.parse
    import httpx

    url = await _resolve_xhs_short_link(url)

    try:
        encoded = urllib.parse.quote(url, safe="")
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{XHS_PROXY}/xhs?url={encoded}")
            if resp.status_code != 200:
                return None, []
            data = resp.json()
    except Exception:
        return None, []

    parts: list[str] = []
    if data.get("title"):
        parts.append(f"标题: {data['title']}")
    if data.get("desc"):
        parts.append(data["desc"][:500])

    text = "\n".join(parts) if parts else None
    images = await _collect_images(data)
    return text, images[:3]


async def fetch_xhs_video_via_proxy(url: str) -> tuple[str | None, list[bytes]]:
    """Fetch XHS video frames via proxy. Returns (text, frame_bytes_list)."""
    import base64
    import urllib.parse
    import httpx

    url = await _resolve_xhs_short_link(url)

    try:
        encoded = urllib.parse.quote(url, safe="")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{XHS_PROXY}/xhs_video?url={encoded}")
            if resp.status_code != 200:
                return None, []
            data = resp.json()
    except Exception:
        return None, []

    parts: list[str] = []
    if data.get("title"):
        parts.append(f"标题: {data['title']}")
    if data.get("desc"):
        parts.append(data["desc"][:500])

    text = "\n".join(parts) if parts else None

    frames: list[bytes] = []
    for item in data.get("frames", []):
        if isinstance(item, str):
            try:
                b64 = item.split(",", 1)[1] if item.startswith("data:") else item
                frames.append(base64.b64decode(b64))
            except Exception:
                pass

    return text, frames[:3]
