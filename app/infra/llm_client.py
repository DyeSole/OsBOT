from __future__ import annotations

from typing import Any

import requests


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _chat_completions_url(self) -> str:
        # Accept both styles:
        # - https://host/v1
        # - https://host/v1/chat/completions
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"
        
    def generate(self, messages: list[dict[str, str]], system_prompt: str) -> str:
        if not self.base_url:
            raise ValueError("missing BASE_URL")
        if not self.api_key:
            raise ValueError("missing API_KEY")

        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self.api_key}",
            "x-api-key": self.api_key,
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}] + messages,
        }

        resp = requests.post(self._chat_completions_url(), headers=headers, json=payload, timeout=45)
        if resp.status_code >= 400:
            snippet = resp.text[:200].replace("\n", " ")
            raise RuntimeError(f"llm http {resp.status_code}: {snippet}")

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("llm response missing choices")

        message = choices[0].get("message") or {}
        content = message.get("content")

        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    texts.append(item["text"])
            joined = "\n".join(texts).strip()
            if joined:
                return joined

        raise RuntimeError("llm response missing message content")
