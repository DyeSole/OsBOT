from __future__ import annotations

from duckduckgo_search import DDGS


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
