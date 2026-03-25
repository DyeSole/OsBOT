from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)


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

    def stream_with_tools(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        on_text: Callable[[str], None],
    ) -> LLMResponse:
        """Stream response, calling on_text for each text delta. Returns full LLMResponse."""
        if not self.base_url:
            raise ValueError("missing BASE_URL")
        if not self.api_key:
            raise ValueError("missing API_KEY")

        payload = self._payload(messages, system_prompt, tools)
        payload["stream"] = True

        resp = requests.post(
            self._request_url(),
            headers=self._headers(),
            json=payload,
            timeout=90,
            stream=True,
        )
        if resp.status_code >= 400:
            snippet = resp.text[:200].replace("\n", " ")
            raise RuntimeError(f"llm http {resp.status_code}: {snippet}")

        resp.encoding = "utf-8"
        if self._is_anthropic_messages_api():
            return self._parse_stream_anthropic(resp, on_text)
        return self._parse_stream_openai(resp, on_text)

    def _parse_stream_anthropic(
        self, resp: requests.Response, on_text: Callable[[str], None],
    ) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        current_tool_name = ""
        current_tool_json = ""

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except (json.JSONDecodeError, ValueError):
                continue
            evt = data.get("type", "")

            if evt == "content_block_start":
                block = data.get("content_block", {})
                if block.get("type") == "tool_use":
                    current_tool_name = block.get("name", "")
                    current_tool_json = ""
            elif evt == "content_block_delta":
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    chunk = delta.get("text", "")
                    if chunk:
                        text_parts.append(chunk)
                        on_text(chunk)
                elif delta.get("type") == "input_json_delta":
                    current_tool_json += delta.get("partial_json", "")
            elif evt == "content_block_stop":
                if current_tool_name:
                    try:
                        args = json.loads(current_tool_json) if current_tool_json else {}
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    tool_calls.append(ToolCall(name=current_tool_name, input=args))
                    current_tool_name = ""
                    current_tool_json = ""

        return LLMResponse(text="".join(text_parts).strip(), tool_calls=tool_calls)

    def _parse_stream_openai(
        self, resp: requests.Response, on_text: Callable[[str], None],
    ) -> LLMResponse:
        text_parts: list[str] = []
        # {index: {"name": str, "args": str}}
        tool_acc: dict[int, dict[str, str]] = {}

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                data = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue
            choices = data.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            content = delta.get("content")
            if content:
                text_parts.append(content)
                on_text(content)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                if idx not in tool_acc:
                    tool_acc[idx] = {"name": "", "args": ""}
                func = tc.get("function") or {}
                if func.get("name"):
                    tool_acc[idx]["name"] = func["name"]
                if func.get("arguments"):
                    tool_acc[idx]["args"] += func["arguments"]

        tool_calls: list[ToolCall] = []
        for _, v in sorted(tool_acc.items()):
            try:
                args = json.loads(v["args"]) if v["args"] else {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            tool_calls.append(ToolCall(name=v["name"], input=args))

        return LLMResponse(text="".join(text_parts).strip(), tool_calls=tool_calls)


class VisionClient:
    """Lightweight client that sends images to a vision model and returns text descriptions."""

    ANTHROPIC_VERSION = "2023-06-01"
    DESCRIBE_PROMPT = "请用简洁的中文描述这张图片的内容，包括画面中的主要元素、场景和氛围。"

    def __init__(self, base_url: str, api_key: str, model: str, *, fallback: "VisionClient | None" = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.fallback = fallback

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def _is_anthropic(self) -> bool:
        return self.base_url.endswith("/messages")

    def _request_url(self) -> str:
        if self._is_anthropic():
            return self.base_url
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self.api_key}",
            "x-api-key": self.api_key,
        }
        if self._is_anthropic():
            headers["anthropic-version"] = self.ANTHROPIC_VERSION
        return headers

    def describe_image(self, image_bytes: bytes, media_type: str) -> str | None:
        """Send an image to the vision model and return a text description.

        Returns None on any failure so callers can gracefully skip.
        """
        if not self.available:
            return None

        b64 = base64.b64encode(image_bytes).decode("ascii")

        try:
            if self._is_anthropic():
                payload = {
                    "model": self.model,
                    "max_tokens": 300,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": b64,
                                    },
                                },
                                {"type": "text", "text": self.DESCRIBE_PROMPT},
                            ],
                        }
                    ],
                }
            else:
                data_url = f"data:{media_type};base64,{b64}"
                payload = {
                    "model": self.model,
                    "max_tokens": 300,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": data_url},
                                },
                                {"type": "text", "text": self.DESCRIBE_PROMPT},
                            ],
                        }
                    ],
                }

            resp = requests.post(
                self._request_url(),
                headers=self._headers(),
                json=payload,
                timeout=120,
            )
            if resp.status_code >= 400:
                log.warning("vision api http %d: %s", resp.status_code, resp.text[:200])
                if self.fallback:
                    log.info("vision falling back to main model")
                    return self.fallback.describe_image(image_bytes, media_type)
                return None

            data = resp.json()

            if self._is_anthropic():
                content = data.get("content", [])
                result = LLMClient._extract_text_from_blocks(content) or None
            else:
                choices = data.get("choices") or []
                if not choices:
                    result = None
                else:
                    msg = choices[0].get("message", {})
                    result = LLMClient._extract_text_from_blocks(msg.get("content")) or None

            if result is None and self.fallback:
                log.info("vision empty result, falling back to main model")
                return self.fallback.describe_image(image_bytes, media_type)
            return result

        except Exception:
            log.exception("vision describe_image failed")
            if self.fallback:
                log.info("vision exception, falling back to main model")
                return self.fallback.describe_image(image_bytes, media_type)
            return None
