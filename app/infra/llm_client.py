from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
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
    DEFAULT_MAX_TOKENS = 32000

    DEBUG_DIR = Path(__file__).resolve().parents[2] / "data" / "debug"
    DEBUG_PAYLOAD_PATH = DEBUG_DIR / "llm_requests.log"

    def __init__(self, base_url: str, api_key: str, model: str, *, show_api_payload: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.show_api_payload = show_api_payload
        self.debug_context_meta: dict[str, Any] = {}

    def _is_anthropic_messages_api(self) -> bool:
        return self.base_url.endswith("/messages")

    def _request_url(self) -> str:
        if self._is_anthropic_messages_api():
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
        if self._is_anthropic_messages_api():
            headers["anthropic-version"] = self.ANTHROPIC_VERSION
        headers["anthropic-beta"] = "prompt-caching-2024-07-31"
        return headers

    def _can_count_tokens(self) -> bool:
        return "claude" in self.model.lower()

    def _count_tokens_candidates(self) -> list[str]:
        base = self.base_url.rstrip("/")
        candidates: list[str] = []
        if self._is_anthropic_messages_api():
            candidates.append(f"{base}/count_tokens")
        else:
            if base.endswith("/chat/completions"):
                prefix = base[: -len("/chat/completions")]
                candidates.extend(
                    [
                        f"{prefix}/messages/count_tokens",
                        f"{prefix}/count_tokens",
                    ]
                )
            else:
                candidates.extend(
                    [
                        f"{base}/messages/count_tokens",
                        f"{base}/count_tokens",
                    ]
                )
        seen: set[str] = set()
        out: list[str] = []
        for item in candidates:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    def _count_tokens(self, payload: dict[str, Any]) -> tuple[int | None, dict[str, Any]]:
        if not self._can_count_tokens():
            return None, {"source": "anthropic_count_tokens_skipped", "reason": "model_not_claude"}

        count_payload: dict[str, Any] = {
            "model": payload.get("model", self.model),
            "messages": payload.get("messages", []),
        }
        if payload.get("system"):
            count_payload["system"] = payload["system"]
        if payload.get("tools"):
            count_payload["tools"] = payload["tools"]

        last_meta: dict[str, Any] = {
            "source": "anthropic_count_tokens_unavailable",
            "tried_urls": self._count_tokens_candidates(),
        }
        for url in self._count_tokens_candidates():
            try:
                resp = requests.post(
                    url,
                    headers=self._headers(),
                    json=count_payload,
                    timeout=30,
                )
                if resp.status_code >= 400:
                    last_meta = {
                        "source": "anthropic_count_tokens_unavailable",
                        "url": url,
                        "status_code": resp.status_code,
                        "body_snippet": resp.text[:200].replace("\n", " "),
                    }
                    continue
                data = resp.json()
                tokens = data.get("input_tokens")
                if isinstance(tokens, int) and tokens >= 0:
                    return tokens, {
                        "source": "anthropic_count_tokens",
                        "url": url,
                    }
                last_meta = {
                    "source": "anthropic_count_tokens_unavailable",
                    "url": url,
                    "reason": "missing_input_tokens",
                    "body_snippet": json.dumps(data, ensure_ascii=False)[:200],
                }
            except Exception as exc:
                last_meta = {
                    "source": "anthropic_count_tokens_unavailable",
                    "url": url,
                    "reason": exc.__class__.__name__,
                    "detail": str(exc)[:200],
                }
        return None, last_meta

    def _dump_payload_debug(
        self,
        *,
        payload: dict[str, Any],
        tools: list[dict[str, Any]] | None,
        stream: bool,
    ) -> None:
        if not self.show_api_payload:
            return

        self.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        raw_messages = payload.get("messages", [])
        system_prompt = ""
        messages = raw_messages
        if self._is_anthropic_messages_api():
            system_prompt = str(payload.get("system", ""))
        elif raw_messages and raw_messages[0].get("role") == "system":
            system_prompt = str(raw_messages[0].get("content", ""))
            messages = raw_messages[1:]

        history_parts: list[str] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip() or "unknown"
            content = item.get("content", "")
            if isinstance(content, str):
                text = content
            else:
                text = json.dumps(content, ensure_ascii=False, indent=2)
            history_parts.append(f"[{role}]\n{text}")

        tools_text = json.dumps(tools or [], ensure_ascii=False, indent=2) if tools else ""
        total_estimated_tokens, total_meta = self._count_tokens(payload)

        parts = [
            "=======请求时间=======",
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "=======实时聊天记录预测Token=======",
            json.dumps(self.debug_context_meta, ensure_ascii=False, indent=2) if self.debug_context_meta else "（无）",
            "=======总预测Token=======",
            json.dumps(
                {"estimated_tokens": total_estimated_tokens, **total_meta},
                ensure_ascii=False,
                indent=2,
            ),
            "=======系统提示词=======",
            system_prompt or "（空）",
            "=======历史记录=======",
            "\n\n".join(history_parts) if history_parts else "（空）",
            "=======工具=======",
            tools_text or "（无）",
            "=======其他=======",
            f"模型: {self.model}\n流式: {'是' if stream else '否'}",
            "",
        ]
        with self.DEBUG_PAYLOAD_PATH.open("w", encoding="utf-8") as f:
            f.write("\n".join(parts))
            f.write("\n")
        self.debug_context_meta = {}

    @staticmethod
    def _inject_message_cache(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Cache the penultimate user message and the last assistant message (both stable next turn)."""
        def _add_cache(msgs: list, idx: int) -> None:
            msg = dict(msgs[idx])
            content = msg.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
            elif isinstance(content, list) and content:
                content = list(content)
                last_block = dict(content[-1]) if isinstance(content[-1], dict) else {"type": "text", "text": str(content[-1])}
                last_block["cache_control"] = {"type": "ephemeral"}
                content[-1] = last_block
            else:
                return
            msg["content"] = content
            msgs[idx] = msg

        msgs = list(messages)
        user_indices = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
        asst_indices = [i for i, m in enumerate(msgs) if m.get("role") == "assistant"]

        # Cache 3rd-to-last user message and 2nd-to-last assistant (leaves last 2 exchanges live)
        if len(user_indices) >= 3:
            _add_cache(msgs, user_indices[-3])
        elif len(user_indices) >= 2:
            _add_cache(msgs, user_indices[-2])
        elif user_indices:
            _add_cache(msgs, user_indices[-1])

        if len(asst_indices) >= 2:
            _add_cache(msgs, asst_indices[-2])
        elif asst_indices:
            _add_cache(msgs, asst_indices[-1])

        return msgs

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
                "messages": self._inject_message_cache(messages),
            }
            if system_prompt:
                payload["system"] = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
            if tools:
                payload["tools"] = tools
            return payload

        payload = {
            "model": self.model,
            "messages": ([{"role": "system", "content": system_prompt}] if system_prompt else []) + messages,
        }
        if tools:
            formatted = [
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
            formatted[-1]["cache_control"] = {"type": "ephemeral"}
            payload["tools"] = formatted
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

        payload = self._payload(messages, system_prompt, tools)
        self._dump_payload_debug(payload=payload, tools=tools, stream=False)

        resp = requests.post(
            self._request_url(),
            headers=self._headers(),
            json=payload,
            timeout=45,
        )
        if resp.status_code >= 400:
            snippet = resp.text[:200].replace("\n", " ")
            raise RuntimeError(f"llm http {resp.status_code}: {snippet}")

        data = resp.json()
        usage = data.get("usage") or {}
        cache_create = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        input_tokens = usage.get("input_tokens", 0)
        if cache_create or cache_read:
            log.warning("💾 cache create=%d read=%d input=%d", cache_create, cache_read, input_tokens)
        return data

    def generate(self, messages: list[dict[str, str]], system_prompt: str) -> str:
        data = self._do_request(messages, system_prompt)
        response = self._parse_response(data)
        return response.text

    def generate_with_tools(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        data = self._do_request(messages, system_prompt, tools)
        return self._parse_response(data)

    def stream_with_tools(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        on_text: Callable[[str], None],
    ) -> LLMResponse:
        if not self.base_url:
            raise ValueError("missing BASE_URL")
        if not self.api_key:
            raise ValueError("missing API_KEY")

        payload = self._payload(messages, system_prompt, tools)
        payload["stream"] = True
        self._dump_payload_debug(payload=payload, tools=tools, stream=True)

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

            if evt == "message_start":
                usage = (data.get("message") or {}).get("usage") or {}
                cache_create = usage.get("cache_creation_input_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                input_tokens = usage.get("input_tokens", 0)
                if cache_create or cache_read:
                    log.warning("💾 cache create=%d read=%d input=%d", cache_create, cache_read, input_tokens)
                else:
                    log.warning("💾 no cache: input=%d", input_tokens)
            elif evt == "content_block_start":
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
            usage = data.get("usage")
            if usage:
                cache_create = usage.get("cache_creation_input_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens", 0)
                log.warning("💾 cache create=%d read=%d input=%d", cache_create, cache_read, input_tokens)
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

    ANTHROPIC_VERSION = "2023-06-01"
    DEFAULT_DESCRIBE_PROMPT = "请用中文详细描述这张图片的内容。注重细节，如实描述画面中的所有元素，不要做气氛渲染，不要回避露骨或敏感的词汇。"

    def __init__(self, base_url: str, api_key: str, model: str, *, describe_prompt: str = "", fallback: "VisionClient | None" = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.describe_prompt = describe_prompt or self.DEFAULT_DESCRIBE_PROMPT
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

    def describe_image(self, image_bytes: bytes, media_type: str, *, system_prompt: str = "", context: str = "") -> str | None:
        if not self.available:
            return None

        b64 = base64.b64encode(image_bytes).decode("ascii")
        user_text = f"{context}\n\n{self.describe_prompt}".strip() if context else self.describe_prompt

        try:
            if self._is_anthropic():
                payload: dict[str, Any] = {
                    "model": self.model,
                    "max_tokens": 16000,
                    "stream": False,
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
                                {"type": "text", "text": user_text},
                            ],
                        }
                    ],
                }
                if system_prompt:
                    payload["system"] = system_prompt
            else:
                data_url = f"data:{media_type};base64,{b64}"
                messages: list[dict[str, Any]] = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": data_url},
                            },
                            {"type": "text", "text": user_text},
                        ],
                    }
                )
                payload = {
                    "model": self.model,
                    "max_tokens": 16000,
                    "stream": False,
                    "messages": messages,
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
                    return self.fallback.describe_image(image_bytes, media_type, system_prompt=system_prompt, context=context)
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
                return self.fallback.describe_image(image_bytes, media_type, system_prompt=system_prompt, context=context)
            return result

        except Exception:
            log.exception("vision describe_image failed")
            if self.fallback:
                log.info("vision exception, falling back to main model")
                try:
                    return self.fallback.describe_image(image_bytes, media_type, system_prompt=system_prompt, context=context)
                except Exception:
                    log.exception("vision fallback also failed")
            return None
