from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

_BV_PATTERN = re.compile(r"BV[a-zA-Z0-9]+")
_VIDEO_API = "https://api.bilibili.com/x/web-interface/view"
_REPLY_API = "https://api.bilibili.com/x/v2/reply"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
_TIMEOUT = 10


def _extract_bvid(url: str) -> str | None:
    m = _BV_PATTERN.search(url)
    return m.group(0) if m else None


async def _resolve_short_url(url: str) -> str:
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=_TIMEOUT) as c:
            resp = await c.head(url)
            return resp.headers.get("location", url)
    except Exception:
        return url


def _is_bilibili_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(
        host == d or host.endswith("." + d)
        for d in ("bilibili.com", "b23.tv")
    )


def _format_count(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


async def fetch_bilibili(url: str) -> str | None:
    if not _is_bilibili_url(url):
        return None

    host = (urlparse(url).hostname or "").lower()
    if "b23.tv" in host:
        url = await _resolve_short_url(url)
        if not _extract_bvid(url):
            return None

    bvid = _extract_bvid(url)
    if not bvid:
        return None

    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT) as client:
        resp = await client.get(_VIDEO_API, params={"bvid": bvid})
        data = resp.json().get("data")
        if not data:
            return None

        title = data.get("title", "")
        author = data.get("owner", {}).get("name", "")
        desc = (data.get("desc", "") or "").strip()
        duration = _format_duration(data.get("duration", 0))
        stat = data.get("stat", {})
        view = _format_count(stat.get("view", 0))
        like = _format_count(stat.get("like", 0))
        coin = _format_count(stat.get("coin", 0))
        reply_count = _format_count(stat.get("reply", 0))
        aid = data.get("aid")

        parts = [
            f"标题: {title}",
            f"UP主: {author}",
            f"时长: {duration}",
            f"播放: {view} | 点赞: {like} | 投币: {coin} | 评论: {reply_count}",
        ]
        if desc and desc != "-":
            parts.append(f"简介: {desc}")

        if aid:
            comments = await _fetch_comments(client, aid)
            if comments:
                parts.append("热门评论:")
                parts.extend(f"  {c}" for c in comments)

        return "\n".join(parts)


async def _fetch_comments(client: httpx.AsyncClient, aid: int, limit: int = 5) -> list[str]:
    try:
        resp = await client.get(_REPLY_API, params={"type": 1, "oid": aid, "sort": 1, "pn": 1, "ps": limit})
        replies = resp.json().get("data", {}).get("replies") or []
        result = []
        for r in replies[:limit]:
            name = r.get("member", {}).get("uname", "")
            text = r.get("content", {}).get("message", "").replace("\n", " ")
            likes = r.get("like", 0)
            if name and text:
                result.append(f"{name}: {text} ({_format_count(likes)}赞)")
        return result
    except Exception:
        return []
