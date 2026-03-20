from __future__ import annotations

from typing import Any

import requests


class LLMClient:
    ANTHROPIC_VERSION = "2023-06-01"
    DEFAULT_MAX_TOKENS = 1024

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _is_anthropic_messages_api(self) -> bool:
        return self.base_url.endswith("/messages")

    def _request_url(self) -> str:
        if self._is_anthropic_messages_api():
            return self.base_url

        # Accept both styles:
        # - https://host/v1
        # - https://host/v1/chat/completions
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self.api_key}",
            "x-api-key": self.api_key,
        }
        if self._is_anthropic_messages_api():
            headers["anthropic-version"] = self.ANTHROPIC_VERSION
        return headers

    def _payload(self, messages: list[dict[str, str]], system_prompt: str) -> dict[str, Any]:
        if self._is_anthropic_messages_api():
            payload: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.DEFAULT_MAX_TOKENS,
                "messages": messages,
            }
            if system_prompt:
                payload["system"] = system_prompt
            return payload

        return {
            "model": self.model,
            "messages": ([{"role": "system", "content": system_prompt}] if system_prompt else []) + messages,
        }

    @staticmethod
    def _extract_text_from_blocks(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    texts.append(item["text"])
            return "\n".join(texts).strip()
        return ""

    def _parse_response_text(self, data: dict[str, Any]) -> str:
        if self._is_anthropic_messages_api():
            text = self._extract_text_from_blocks(data.get("content"))
            if text:
                return text
            raise RuntimeError("llm response missing content")

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("llm response missing choices")

        message = choices[0].get("message") or {}
        text = self._extract_text_from_blocks(message.get("content"))
        if text:
            return text

        raise RuntimeError("llm response missing message content")

    def generate(self, messages: list[dict[str, str]], system_prompt: str) -> str:
        if not self.base_url:
            raise ValueError("missing BASE_URL")
        if not self.api_key:
            raise ValueError("missing API_KEY")

        resp = requests.post(
            self._request_url(),
            headers=self._headers(),
            json=self._payload(messages, system_prompt),
            timeout=45,
        )
        if resp.status_code >= 400:
            snippet = resp.text[:200].replace("\n", " ")
            raise RuntimeError(f"llm http {resp.status_code}: {snippet}")

        data = resp.json()
        return self._parse_response_text(data)
