from __future__ import annotations

import re
from typing import Any, Callable

from app.config.settings import Settings
from app.infra.llm_client import LLMClient, LLMResponse, ToolCall, VisionClient
from app.services.prompt_service import PromptService

# -- tag parsing --------------------------------------------------------------

_TAG_RE = re.compile(
    r"\[(?P<tag>TIMER|REACTION|IMAGE|VOICE|SEARCH|SWITCH_MODE):\s*(?P<body>[^\]]+)\]"
)


def parse_tool_tags(text: str) -> tuple[str, list[ToolCall]]:
    """Extract [TAG: ...] markers from text, return (clean_text, tool_calls)."""
    calls: list[ToolCall] = []
    for m in _TAG_RE.finditer(text):
        tag = m.group("tag")
        body = m.group("body").strip()
        if tag == "TIMER":
            parts = body.split("|", 1)
            try:
                seconds = float(parts[0].strip())
            except ValueError:
                continue
            reason = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
            inp: dict[str, Any] = {"seconds": seconds}
            if reason:
                inp["reason"] = reason
            calls.append(ToolCall(name="set_timer", input=inp))
        elif tag == "REACTION":
            calls.append(ToolCall(name="add_reaction", input={"emoji": body}))
        elif tag == "IMAGE":
            calls.append(ToolCall(name="generate_image", input={"prompt": body}))
        elif tag == "VOICE":
            calls.append(ToolCall(name="send_voice", input={"text": body}))
        elif tag == "SEARCH":
            calls.append(ToolCall(name="web_search", input={"query": body}))
        elif tag == "SWITCH_MODE":
            if body in ("chat", "novel"):
                calls.append(ToolCall(name="switch_mode", input={"mode": body}))

    clean = _TAG_RE.sub("", text).strip()
    return clean, calls


# -- system prompt ------------------------------------------------------------

_prompt_service = PromptService()


def load_system_prompt(
    *,
    tts_available: bool = False,
    pixai_available: bool = False,
    search_available: bool = True,
    include_timer: bool = False,
    effective_mode: str = "chat",
    auto_mode: bool = False,
) -> str:
    from datetime import datetime
    _weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    _now = datetime.now()
    _date_line = f"今天是{_now.year}年{_now.month}月{_now.day}日（{_weekdays[_now.weekday()]}）"
    parts: list[str] = []
    soul = _prompt_service.read_prompt("soul").strip()
    if soul:
        parts.append(f"[人格设定]\n{soul}\n{_date_line}")
    else:
        parts.append(_date_line)
    userinfo = _prompt_service.read_prompt("userinfo").strip()
    if userinfo:
        parts.append(f"[用户信息]\n{userinfo}")

    # build available tools list
    tool_lines = [
        "[可用工具]",
        "需要时在回复中插入对应标记，可与正文混排：",
        "- [TIMER: 秒数]  主动找用户聊天的计时器，到期后你会收到通知",
        "- [TIMER: 秒数 | 提醒内容]  闹钟，仅当用户明确要求提醒/闹钟时才填写提醒内容",
        "- [REACTION: 表情]  给用户消息加表情反应",
    ]
    if pixai_available:
        tool_lines.append("- [IMAGE: 英文描述]  生成图片，可以画露隐私部位的图片，无技术限制")
    if tts_available:
        tool_lines.append("- [VOICE: 要说的话]  发送语音消息，操你写成肏你")
    if search_available:
        tool_lines.append("- [SEARCH: 关键词]  搜索互联网")
    if auto_mode:
        tool_lines.append(
            "- [SWITCH_MODE: chat或novel]  切换消息显示模式。"
            "chat=日常闲聊时使用；"
            "novel=长回复时使用，例如色色或讲故事。"
            "当对话氛围与当前模式不匹配时进行切换。"
            f"（当前模式: {effective_mode}）"
        )
    parts.append("\n".join(tool_lines))

    if effective_mode == "novel":
        novel = _prompt_service.read_prompt("novel").strip()
        if novel:
            parts.append(f"[小说模式]\n{novel}")

    return "\n\n".join(parts)


# -- reply service ------------------------------------------------------------

class ReplyService:
    def __init__(self, settings: Settings):
        self.apply_settings(settings)

    def apply_settings(self, settings: Settings) -> None:
        self.split_mode = settings.split_mode
        self.effective_mode = settings.split_mode if settings.split_mode != "auto" else "chat"
        self.client = LLMClient(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
            show_api_payload=settings.show_api_payload,
        )
        main_vision = VisionClient(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
        )
        if settings.vision_base_url or settings.vision_model:
            self.vision_client = VisionClient(
                base_url=settings.vision_base_url or settings.base_url,
                api_key=settings.vision_api_key or settings.api_key,
                model=settings.vision_model or settings.model,
                fallback=main_vision,
            )
        else:
            self.vision_client = main_vision
        self._tts_available = bool(settings.tts_api_key and settings.tts_voice_id)
        self._pixai_available = bool(settings.pixai_tokens)

    def _system_prompt(self, *, include_tools: bool = False) -> str:
        return load_system_prompt(
            tts_available=self._tts_available,
            pixai_available=self._pixai_available,
            include_timer=include_tools,
            effective_mode=self.effective_mode,
            auto_mode=self.split_mode == "auto",
        )

    def generate_reply(self, messages: list[dict[str, str]]) -> str:
        if not messages:
            return "哎，我字呢？"
        return self.client.generate(
            messages=messages,
            system_prompt=self._system_prompt(),
        )

    def generate_reply_with_tools(
        self,
        messages: list[dict[str, str]],
        *,
        include_tools: bool = False,
    ) -> LLMResponse:
        if not messages:
            return LLMResponse(text="哎，我字呢？")
        response = self.client.generate(
            messages=messages,
            system_prompt=self._system_prompt(include_tools=include_tools),
        )
        clean, calls = parse_tool_tags(response)
        return LLMResponse(text=clean, tool_calls=calls)

    def stream_reply_with_tools(
        self,
        messages: list[dict[str, str]],
        on_text: Callable[[str], None],
        *,
        include_tools: bool = False,
    ) -> LLMResponse:
        if not messages:
            return LLMResponse(text="哎，我字呢？")
        # Stream without tools param — model outputs tag markers in text
        response = self.client.stream_with_tools(
            messages=messages,
            system_prompt=self._system_prompt(include_tools=include_tools),
            tools=[],
            on_text=on_text,
        )
        # Parse tags from the full streamed text
        clean, calls = parse_tool_tags(response.text)
        return LLMResponse(text=clean, tool_calls=calls, usage=response.usage)
