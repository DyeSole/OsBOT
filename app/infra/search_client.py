from __future__ import annotations

import asyncio

from duckduckgo_search import DDGS

# Limit concurrent browser-heavy operations (e.g. future Playwright usage).
# DuckDuckGo HTTP search is lightweight, but this protects against memory
# spikes if browser-based fetching is added later.
_browser_semaphore = asyncio.Semaphore(1)


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


async def browser_fetch(coro):
    """Run a browser-heavy coroutine with concurrency=1 to protect memory."""
    async with _browser_semaphore:
        return await coro
