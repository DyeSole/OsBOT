from __future__ import annotations

import json
import urllib.request
import urllib.parse

from duckduckgo_search import DDGS

_SITE_DOMAINS: dict[str, str] = {
    "xiaohongshu": "xiaohongshu.com",
    "xhs": "xiaohongshu.com",
    "小红书": "xiaohongshu.com",
    "x": "x.com",
    "twitter": "x.com",
    "bilibili": "bilibili.com",
    "b站": "bilibili.com",
}


def web_search(query: str, *, max_results: int = 5) -> list[dict[str, str]]:
    """Search the web via DuckDuckGo and return a list of results.

    Each result dict has keys: title, href, body.
    """
    with DDGS() as ddgs:
        raw = list(ddgs.text(query, max_results=max_results))
    return [
        {
            "title": r.get("title", ""),
            "href": r.get("href", ""),
            "body": r.get("body", ""),
        }
        for r in raw
    ]


def site_search(
    query: str,
    site: str,
    *,
    max_results: int = 5,
) -> list[dict[str, str]]:
    """Search within a specific site (xiaohongshu / x / bilibili).

    Falls back to DuckDuckGo site: search for all platforms.
    Bilibili additionally tries its public API for richer results.
    """
    domain = _SITE_DOMAINS.get(site.lower(), site.lower())

    if domain == "bilibili.com":
        results = _bilibili_search(query, max_results=max_results)
        if results:
            return results

    return web_search(f"site:{domain} {query}", max_results=max_results)


def _bilibili_search(
    query: str, *, max_results: int = 5,
) -> list[dict[str, str]]:
    """Search Bilibili using its public API."""
    params = urllib.parse.urlencode({
        "keyword": query,
        "page": 1,
        "page_size": max_results,
        "search_type": "video",
    })
    url = f"https://api.bilibili.com/x/web-interface/search/type?{params}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        items = data.get("data", {}).get("result", [])
        return [
            {
                "title": _strip_tags(item.get("title", "")),
                "href": f"https://www.bilibili.com/video/{item.get('bvid', '')}",
                "body": item.get("description", ""),
            }
            for item in items[:max_results]
        ]
    except Exception:
        return []


def _strip_tags(text: str) -> str:
    """Remove HTML tags from Bilibili search highlights."""
    import re
    return re.sub(r"<[^>]+>", "", text)
