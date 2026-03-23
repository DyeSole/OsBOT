from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


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

    def _payload(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if self._is_anthropic_messages_api():
            payload: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.DEFAULT_MAX_TOKENS,
                "messages": messages,
            }
            if system_prompt:
                payload["system"] = system_prompt
            if tools:
                payload["tools"] = tools
            return payload

        payload = {
            "model": self.model,
            "messages": ([{"role": "system", "content": system_prompt}] if system_prompt else []) + messages,
        }
        if tools:
            # OpenAI-compatible format
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
                for t in tools
            ]
        return payload

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

    @staticmethod
    def _extract_tool_calls_anthropic(content: Any) -> list[ToolCall]:
        if not isinstance(content, list):
            return []
        calls: list[ToolCall] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                calls.append(ToolCall(
                    name=item.get("name", ""),
                    input=item.get("input", {}),
                ))
        return calls

    @staticmethod
    def _extract_tool_calls_openai(message: dict[str, Any]) -> list[ToolCall]:
        raw_calls = message.get("tool_calls") or []
        calls: list[ToolCall] = []
        for tc in raw_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function") or {}
            name = func.get("name", "")
            args_raw = func.get("arguments", "{}")
            import json
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append(ToolCall(name=name, input=args))
        return calls

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        if self._is_anthropic_messages_api():
            content = data.get("content")
            text = self._extract_text_from_blocks(content)
            tool_calls = self._extract_tool_calls_anthropic(content)
            if not text and not tool_calls:
                raise RuntimeError("llm response missing content")
            return LLMResponse(text=text, tool_calls=tool_calls)

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("llm response missing choices")

        message = choices[0].get("message") or {}
        text = self._extract_text_from_blocks(message.get("content"))
        tool_calls = self._extract_tool_calls_openai(message)
        if not text and not tool_calls:
            raise RuntimeError("llm response missing message content")
        return LLMResponse(text=text, tool_calls=tool_calls)

    def _do_request(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise ValueError("missing BASE_URL")
        if not self.api_key:
            raise ValueError("missing API_KEY")

        resp = requests.post(
            self._request_url(),
            headers=self._headers(),
            json=self._payload(messages, system_prompt, tools),
            timeout=45,
        )
        if resp.status_code >= 400:
            snippet = resp.text[:200].replace("\n", " ")
            raise RuntimeError(f"llm http {resp.status_code}: {snippet}")

        return resp.json()

    def generate(self, messages: list[dict[str, str]], system_prompt: str) -> str:
        """Backwards-compatible: returns text only."""
        data = self._do_request(messages, system_prompt)
        response = self._parse_response(data)
        return response.text

    def generate_with_tools(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """Returns structured response with text and tool calls."""
        data = self._do_request(messages, system_prompt, tools)
        return self._parse_response(data)
