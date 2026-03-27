from __future__ import annotations

import json
from pathlib import Path

import httpx

_AUTH_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "browser" / "auth" / "xiaohongshu.json"
_SEARCH_API = "https://edith.xiaohongshu.com/api/sns/web/v1/search/notes"
_TIMEOUT = 15

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.xiaohongshu.com/",
    "Origin": "https://www.xiaohongshu.com",
    "Content-Type": "application/json",
}


def _load_cookies() -> dict[str, str]:
    try:
        data = json.loads(_AUTH_FILE.read_text())
        return {c["name"]: c["value"] for c in data.get("cookies", [])}
    except Exception:
        return {}


async def search_notes(keyword: str, count: int = 5, offset: int = 0) -> list[dict]:
    """Search XHS notes. Returns list of {title, url, desc, likes}."""
    cookies = _load_cookies()
    if not cookies.get("a1") or not cookies.get("web_session"):
        return []

    page = offset // 20 + 1
    payload = {
        "keyword": keyword,
        "page": page,
        "page_size": min(count, 20),
        "search_id": "",
        "sort": "general",
        "note_type": 0,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, cookies=cookies) as client:
            resp = await client.post(_SEARCH_API, json=payload, headers=_HEADERS)
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception:
        return []

    if data.get("code") != 0:
        return []

    results = []
    items = data.get("data", {}).get("items", [])
    for item in items:
        note = item.get("note_card") or item
        note_id = item.get("id") or note.get("note_id") or ""
        title = note.get("display_title") or note.get("title") or ""
        desc = note.get("desc", "")
        likes = note.get("interact_info", {}).get("liked_count", "")
        url = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ""
        if title or url:
            results.append({"title": title, "url": url, "desc": desc, "likes": likes})
        if len(results) >= count:
            break

    return results
