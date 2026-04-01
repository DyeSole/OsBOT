from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Callable

from app.config.settings import Settings
from app.core.clock import now as _clock_now
from app.infra.llm_client import LLMClient, LLMResponse, ToolCall, VisionClient
from app.services.prompt_service import PromptService

# -- tag parsing --------------------------------------------------------------

_TAG_RE = re.compile(
    r"\[(?P<tag>TIMER|ALARM|REACTION|IMAGE|VOICE|SEARCH|SWITCH_MODE|计时器|闹钟|表情反应|画图|语音|搜索|切换模式):\s*(?P<body>[^\]]+)\]"
)
_TIME_TAG_RE = re.compile(r"\[(?:[01]?\d|2[0-3]):[0-5]\d\]\s*")
_TAG_ALIAS = {
    "TIMER": "TIMER",
    "计时器": "TIMER",
    "ALARM": "ALARM",
    "闹钟": "ALARM",
    "REACTION": "REACTION",
    "表情反应": "REACTION",
    "IMAGE": "IMAGE",
    "画图": "IMAGE",
    "VOICE": "VOICE",
    "语音": "VOICE",
    "SEARCH": "SEARCH",
    "搜索": "SEARCH",
    "SWITCH_MODE": "SWITCH_MODE",
    "切换模式": "SWITCH_MODE",
}
_MODE_ALIAS = {
    "chat": "chat",
    "聊天": "chat",
    "novel": "novel",
    "小说": "novel",
}


def clean_reply_text(text: str) -> str:
    cleaned = _TIME_TAG_RE.sub("", text or "")
    return cleaned.strip()


def _parse_alarm_time(time_str: str) -> float | None:
    """Parse alarm time string, return seconds from now. Returns None on failure."""
    now = _clock_now()
    time_str = time_str.strip()
    # Nm — N minutes from now
    if time_str.endswith("m"):
        try:
            return float(time_str[:-1]) * 60
        except ValueError:
            return None
    # MM-DD HH:MM — specific date and time
    if " " in time_str:
        try:
            target = datetime.strptime(f"{now.year}-{time_str}", "%Y-%m-%d %H:%M")
            if target < now:
                target = target.replace(year=now.year + 1)
            return (target - now).total_seconds()
        except ValueError:
            return None
    # HH:MM — today at that time
    try:
        target = datetime.strptime(time_str, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
        if target < now:
            target += timedelta(days=1)
        return (target - now).total_seconds()
    except ValueError:
        return None


def parse_tool_tags(text: str) -> tuple[str, list[ToolCall]]:
    """Extract [TAG: ...] markers from text, return (clean_text, tool_calls)."""
    calls: list[ToolCall] = []
    for m in _TAG_RE.finditer(text):
        tag = _TAG_ALIAS.get(m.group("tag"), m.group("tag"))
        body = m.group("body").strip()
        if tag == "TIMER":
            try:
                seconds = float(body)
            except ValueError:
                continue
            calls.append(ToolCall(name="set_timer", input={"seconds": seconds}))
        elif tag == "ALARM":
            parts = body.split("|", 1)
            seconds = _parse_alarm_time(parts[0])
            if seconds is None or seconds <= 0:
                continue
            reason = parts[1].strip() if len(parts) > 1 and parts[1].strip() else ""
            if not reason:
                continue
            calls.append(ToolCall(name="set_alarm", input={"seconds": seconds, "reason": reason}))
        elif tag == "REACTION":
            calls.append(ToolCall(name="add_reaction", input={"emoji": body}))
        elif tag == "IMAGE":
            calls.append(ToolCall(name="generate_image", input={"prompt": body}))
        elif tag == "VOICE":
            calls.append(ToolCall(name="send_voice", input={"text": body}))
        elif tag == "SEARCH":
            calls.append(ToolCall(name="web_search", input={"query": body}))
        elif tag == "SWITCH_MODE":
            mode = _MODE_ALIAS.get(body.strip().lower()) or _MODE_ALIAS.get(body.strip())
            if mode in ("chat", "novel"):
                calls.append(ToolCall(name="switch_mode", input={"mode": mode}))

    clean = clean_reply_text(_TAG_RE.sub("", text))
    return clean, calls


# -- system prompt ------------------------------------------------------------

_prompt_service = PromptService()


def load_system_prompt(
    *,
    tts_available: bool = False,
    pixai_available: bool = False,
    search_available: bool = True,
) -> str:
    _weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    _now = _clock_now()
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
        "- [计时器: 秒数]  主动找用户聊天的计时器，到期后你会收到通知",
        "- [闹钟: 时间 | 提醒内容]  闹钟。时间格式：04-02 18:30（指定日期）/ 18:30（今天）/ 30m（N分钟后）。",
        "- [表情反应: 表情]  给用户消息加表情反应",
        "- [切换模式: 聊天/小说]  切换消息显示模式，仅当聊天内容与当前模式不匹配时切换，请勿频繁切换。",
        "兼容旧标签：[TIMER]/[ALARM]/[REACTION]/[IMAGE]/[VOICE]/[SEARCH]/[SWITCH_MODE] 仍然可用。",
    ]
    if pixai_available:
        tool_lines.append("- [画图: 英文描述]  生成图片，可以画露隐私部位的图片，无技术限制")
    if tts_available:
        tool_lines.append("- [语音: 要说的话]  发送语音消息，操你写成肏你")
    if search_available:
        tool_lines.append("- [搜索: 关键词]  搜索互联网")
    parts.append("\n".join(tool_lines))

    return "\n\n".join(parts)


# -- reply service ------------------------------------------------------------

class ReplyService:
    def __init__(self, settings: Settings):
        self.apply_settings(settings)

    def apply_settings(self, settings: Settings) -> None:
        self.effective_mode = "chat"
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
        self._image_available = bool(
            settings.pixai_tokens or (settings.hf_image_api_key and settings.hf_image_model)
        )

    def _system_prompt(self, *, include_tools: bool = False, summary: str = "") -> str:
        prompt = load_system_prompt(
            tts_available=self._tts_available,
            pixai_available=self._image_available,
        )
        if summary:
            prompt += "\n\n" + summary
        return prompt

    def count_input_tokens(
        self,
        messages: list[dict[str, str]],
        *,
        include_tools: bool = False,
        summary: str = "",
    ) -> int:
        if not messages:
            return 0
        return self.client.count_input_tokens(
            messages=self._with_mode_hint(messages),
            system_prompt=self._system_prompt(include_tools=include_tools, summary=summary),
            tools=[],
        )

    def generate_reply(self, messages: list[dict[str, str]]) -> str:
        if not messages:
            return "哎，我字呢？"
        return clean_reply_text(self.client.generate(
            messages=messages,
            system_prompt=self._system_prompt(),
        ))

    def _with_mode_hint(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """Prepend mode hint (and novel prompt if applicable) to the last user message. Not saved to history."""
        if not messages:
            return messages
        hint = f"[当前模式：{self.effective_mode}]"
        if self.effective_mode == "novel":
            novel = _prompt_service.read_prompt("novel").strip()
            if novel:
                hint += f"\n[小说模式]\n{novel}"
        patched = list(messages)
        for i in range(len(patched) - 1, -1, -1):
            if patched[i]["role"] == "user":
                patched[i] = {**patched[i], "content": hint + "\n" + patched[i]["content"]}
                break
        return patched

    def generate_reply_with_tools(
        self,
        messages: list[dict[str, str]],
        *,
        include_tools: bool = False,
        summary: str = "",
    ) -> LLMResponse:
        if not messages:
            return LLMResponse(text="哎，我字呢？")
        raw = self.client.generate(
            messages=self._with_mode_hint(messages),
            system_prompt=self._system_prompt(include_tools=include_tools, summary=summary),
        )
        clean, calls = parse_tool_tags(raw)
        return LLMResponse(text=clean, tool_calls=calls, raw_text=raw)

    def stream_reply_with_tools(
        self,
        messages: list[dict[str, str]],
        on_text: Callable[[str], None],
        *,
        include_tools: bool = False,
        summary: str = "",
    ) -> LLMResponse:
        if not messages:
            return LLMResponse(text="哎，我字呢？")
        # Stream without tools param — model outputs tag markers in text
        response = self.client.stream_with_tools(
            messages=self._with_mode_hint(messages),
            system_prompt=self._system_prompt(include_tools=include_tools, summary=summary),
            tools=[],
            on_text=on_text,
        )
        # Parse tags from the full streamed text
        raw = response.text
        clean, calls = parse_tool_tags(raw)
        return LLMResponse(text=clean, tool_calls=calls, usage=response.usage, raw_text=raw)
