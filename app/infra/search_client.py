from __future__ import annotations

import json
import re

import httpx
from duckduckgo_search import DDGS


def web_search(
    query: str,
    *,
    max_results: int = 5,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    context: str = "",
) -> list[dict[str, str]]:
    """Search the web. Uses Grok/xAI if configured, otherwise DuckDuckGo."""
    if base_url and api_key:
        return _grok_search(query, base_url=base_url, api_key=api_key, model=model, max_results=max_results, context=context)
    return _ddg_search(query, max_results=max_results)


def _ddg_search(query: str, *, max_results: int = 5) -> list[dict[str, str]]:
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


def _grok_search(
    query: str,
    *,
    base_url: str,
    api_key: str,
    model: str = "",
    max_results: int = 5,
    context: str = "",
) -> list[dict[str, str]]:
    """Use Grok/xAI API with web search to get results."""
    use_model = model or "grok-4.1-fast"

    system_prompt = (
        f"你是一个搜索助手。请根据用户的搜索关键词进行联网搜索，返回最多{max_results}条结果。"
        "必须严格按以下JSON格式返回，不要包含其他内容：\n"
        '[{"title": "标题", "href": "链接URL", "body": "摘要"}]'
    )
    if context:
        system_prompt += f"\n\n以下是用户最近的聊天记录，帮助你理解搜索意图：\n{context}"

    # Use httpx directly to ensure search_parameters is sent correctly
    url = f"{base_url.rstrip('/')}/chat/completions"
    resp = httpx.post(
        url,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        json={
            "model": use_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            "search_parameters": {"mode": "auto"},
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    text = (data["choices"][0]["message"]["content"] or "").strip()
    # Extract JSON array from response
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group())
    except (json.JSONDecodeError, TypeError):
        return []
    return [
        {
            "title": str(item.get("title", "")),
            "href": str(item.get("href", "")),
            "body": str(item.get("body", "")),
        }
        for item in items
        if isinstance(item, dict)
    ][:max_results]
