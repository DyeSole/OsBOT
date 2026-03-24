from __future__ import annotations

from typing import Any, Callable

from app.config.settings import Settings
from app.infra.llm_client import LLMClient, LLMResponse, VisionClient
from app.services.prompt_service import PromptService

# Available in normal conversation — only for user-requested alarms
ALARM_TOOLS: list[dict[str, Any]] = [
    {
        "name": "set_timer",
        "description": (
            "仅当用户明确要求你设置闹钟或提醒时才使用此工具，其他任何情况都禁止调用。"
            "时间单位为秒。例如想设 30 分钟就传 1800。"
            "必须在 reason 中写明提醒内容，到期后你必须把这件事告诉用户，不可以沉默。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "计时器时长（秒），不限范围。",
                },
                "reason": {
                    "type": "string",
                    "description": "提醒内容。必填。",
                },
            },
            "required": ["seconds", "reason"],
        },
    },
]

SEARCH_TOOL: dict[str, Any] = {
    "name": "web_search",
    "description": (
        "使用搜索引擎搜索互联网上的信息。"
        "当你需要查找最新信息、不确定的事实、或用户明确要求你搜索时使用。"
        "返回搜索结果列表，包含标题、链接和摘要。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词。",
            },
        },
        "required": ["query"],
    },
}

# Available during timer/alarm fires — bot can also set voluntary timers
TIMER_TOOLS: list[dict[str, Any]] = [
    {
        "name": "set_timer",
        "description": (
            "设置一个计时器。计时器到期后你会收到通知，届时你可以选择对用户说话或保持沉默。"
            "时间单位为秒。例如想设 30 分钟就传 1800。"
            "如果用户明确要求你提醒他某件事，请在 reason 中写明提醒内容，"
            "到期后你必须把这件事告诉用户，不可以沉默。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "计时器时长（秒）。没有 reason 时范围为 120~7200（2分钟~2小时），有 reason 时不限。",
                },
                "reason": {
                    "type": "string",
                    "description": "提醒内容。仅当用户明确要求你提醒/闹钟时才填写，其他任何情况都不要填这个字段。",
                },
            },
            "required": ["seconds"],
        },
    },
]


_prompt_service = PromptService()


def load_system_prompt() -> str:
    parts: list[str] = []
    soul = _prompt_service.read_prompt("soul").strip()
    if soul:
        parts.append(f"[人格设定]\n{soul}")
    userinfo = _prompt_service.read_prompt("userinfo").strip()
    if userinfo:
        parts.append(f"[用户信息]\n{userinfo}")
    return "\n\n".join(parts)


class ReplyService:
    def __init__(self, settings: Settings):
        self.apply_settings(settings)

    def apply_settings(self, settings: Settings) -> None:
        self.client = LLMClient(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
        )
        self.vision_client = VisionClient(
            base_url=settings.vision_base_url or settings.base_url,
            api_key=settings.vision_api_key or settings.api_key,
            model=settings.vision_model or settings.model,
        )

    def generate_reply(self, messages: list[dict[str, str]]) -> str:
        if not messages:
            return "哎，我字呢？"

        return self.client.generate(messages=messages, system_prompt=load_system_prompt())

    def generate_reply_with_tools(
        self,
        messages: list[dict[str, str]],
        *,
        include_tools: bool = False,
        include_search: bool = True,
    ) -> LLMResponse:
        if not messages:
            return LLMResponse(text="哎，我字呢？")

        tools = TIMER_TOOLS if include_tools else ALARM_TOOLS
        if include_search:
            tools = tools + [SEARCH_TOOL]
        return self.client.generate_with_tools(
            messages=messages,
            system_prompt=load_system_prompt(),
            tools=tools,
        )

    def stream_reply_with_tools(
        self,
        messages: list[dict[str, str]],
        on_text: Callable[[str], None],
        *,
        include_tools: bool = False,
    ) -> LLMResponse:
        if not messages:
            return LLMResponse(text="哎，我字呢？")

        return self.client.stream_with_tools(
            messages=messages,
            system_prompt=load_system_prompt(),
            tools=(TIMER_TOOLS if include_tools else ALARM_TOOLS) + [SEARCH_TOOL],
            on_text=on_text,
        )
