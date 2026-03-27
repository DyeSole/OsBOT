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


async def fetch_bilibili_comments(url: str, pages: int = 3) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    if "b23.tv" in host:
        url = await _resolve_short_url(url)

    bvid = _extract_bvid(url)
    if not bvid:
        return None

    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT) as client:
        resp = await client.get(_VIDEO_API, params={"bvid": bvid})
        data = resp.json().get("data")
        if not data:
            return None

        aid = data.get("aid")
        if not aid:
            return None

        title = data.get("title", "")
        all_comments: list[str] = []
        for pn in range(1, pages + 1):
            try:
                resp = await client.get(_REPLY_API, params={"type": 1, "oid": aid, "sort": 1, "pn": pn, "ps": 20})
                replies = resp.json().get("data", {}).get("replies") or []
                if not replies:
                    break
                for r in replies:
                    name = r.get("member", {}).get("uname", "")
                    text = r.get("content", {}).get("message", "").replace("\n", " ")
                    likes = r.get("like", 0)
                    if name and text:
                        all_comments.append(f"{name}: {text} ({_format_count(likes)}赞)")
            except Exception:
                break

        if not all_comments:
            return None

        parts = [f"「{title}」的评论（共{len(all_comments)}条）:"]
        parts.extend(all_comments)
        return "\n".join(parts)


_SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
_NAV_API = "https://api.bilibili.com/x/web-interface/nav"
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]
_wbi_cache: dict = {}


async def _get_mixin_key(client: httpx.AsyncClient) -> str:
    import time
    cached = _wbi_cache.get("key")
    ts = _wbi_cache.get("ts", 0)
    if cached and time.time() - ts < 3600:
        return cached
    resp = await client.get(_NAV_API, timeout=_TIMEOUT)
    wbi = resp.json()["data"]["wbi_img"]
    img_key = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
    sub_key = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
    s = img_key + sub_key
    key = "".join(s[i] for i in _MIXIN_KEY_ENC_TAB)[:32]
    _wbi_cache["key"] = key
    _wbi_cache["ts"] = time.time()
    return key


async def _sign(params: dict, client: httpx.AsyncClient) -> dict:
    import hashlib, time, urllib.parse
    mixin_key = await _get_mixin_key(client)
    params = dict(params)
    params["wts"] = int(time.time())
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params


async def search_bilibili(keyword: str, count: int = 5, offset: int = 0) -> list[dict]:
    """Search B站 videos. Returns list of {title, bvid, url, play, duration, up}."""
    import re as _re
    page = offset // 20 + 1
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT) as client:
        params = await _sign(
            {"keyword": keyword, "search_type": "video", "page": page, "page_size": 20},
            client,
        )
        resp = await client.get(_SEARCH_API, params=params)
        data = resp.json()
        if data.get("code") != 0:
            return []
        results = data.get("data", {}).get("result") or []
        out = []
        for r in results[offset % 20: offset % 20 + count]:
            title = _re.sub("<[^>]+>", "", r.get("title", ""))
            bvid = r.get("bvid", "")
            if not bvid:
                continue
            out.append({
                "title": title,
                "bvid": bvid,
                "url": f"https://www.bilibili.com/video/{bvid}",
                "play": _format_count(r.get("play") or 0),
                "duration": r.get("duration", ""),
                "up": r.get("author", ""),
            })
        return out[:count]
