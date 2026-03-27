from __future__ import annotations

from typing import Any, Callable

from app.config.settings import Settings
from app.infra.llm_client import LLMClient, LLMResponse, VisionClient
from app.services.prompt_service import PromptService

ALARM_TOOLS: list[dict[str, Any]] = [
    {
        "name": "set_timer",
        "description": "用户要求设闹钟/提醒时用。seconds单位秒。reason填提醒内容，到期必须告知用户。",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "description": "秒数"},
                "reason": {"type": "string", "description": "提醒内容"},
            },
            "required": ["seconds", "reason"],
        },
    },
]

SEARCH_TOOL: dict[str, Any] = {
    "name": "web_search",
    "description": "搜索互联网。查最新信息或不确定事实时用。",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词"},
        },
        "required": ["query"],
    },
}

REACTION_TOOL: dict[str, Any] = {
    "name": "add_reaction",
    "description": "给用户消息加表情。表情胜过文字时用。",
    "input_schema": {
        "type": "object",
        "properties": {
            "emoji": {"type": "string", "description": "表情符号"},
        },
        "required": ["emoji"],
    },
}

READ_COMMENTS_TOOL: dict[str, Any] = {
    "name": "read_comments",
    "description": "读取链接评论区。",
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "完整URL"},
        },
        "required": ["url"],
    },
}


SEARCH_BILIBILI_TOOL: dict[str, Any] = {
    "name": "search_bilibili",
    "description": "B站搜视频，返标题/播放量/时长/UP主/链接。搜完必须发链接给用户。",
    "input_schema": {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "关键词"},
            "count": {"type": "integer", "minimum": 1, "maximum": 10},
            "offset": {"type": "integer", "minimum": 0},
        },
        "required": ["keyword", "count"],
    },
}


SEARCH_XHS_TOOL: dict[str, Any] = {
    "name": "search_xiaohongshu",
    "description": "小红书搜帖子，返标题/点赞数/链接。搜完必须发链接。最多连续5次翻页。",
    "input_schema": {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "关键词"},
            "count": {"type": "integer", "minimum": 0, "maximum": 10},
            "offset": {"type": "integer", "minimum": 0},
        },
        "required": ["keyword", "count"],
    },
}



def _build_speak_tool() -> dict[str, Any]:
    extra = _prompt_service.read_prompt("tts").strip()
    desc = "文字转语音。撒娇/表白/道晚安等时机用，不要每条都用。text不带markdown/URL。"
    if extra:
        desc = desc + "\n\n" + extra
    return {
        "name": "speak",
        "description": desc,
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "朗读文字，≤100字"},
                "emotion": {
                    "type": "string",
                    "description": "情绪，不填用默认",
                    "enum": ["happy", "sad", "angry", "calm", "surprised", "fearful"],
                },
            },
            "required": ["text"],
        },
    }


TIMER_TOOLS: list[dict[str, Any]] = [
    {
        "name": "set_timer",
        "description": "设计时器，到期收通知可选说话或沉默。seconds单位秒，120~7200；有reason时到期必须告知用户。",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "description": "秒数，120~7200；有reason时不限"},
                "reason": {"type": "string", "description": "提醒内容，仅用户要求提醒时填"},
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
    def __init__(self, settings: Settings, *, describe_prompt: str = ""):
        self.apply_settings(settings, describe_prompt=describe_prompt)

    def apply_settings(self, settings: Settings, *, describe_prompt: str = "") -> None:
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
            describe_prompt=describe_prompt,
        )
        if settings.vision_base_url or settings.vision_model:
            self.vision_client = VisionClient(
                base_url=settings.vision_base_url or settings.base_url,
                api_key=settings.vision_api_key or settings.api_key,
                model=settings.vision_model or settings.model,
                describe_prompt=describe_prompt,
                fallback=main_vision,
            )
        else:
            self.vision_client = main_vision

    def set_debug_context_meta(self, *, estimated_tokens: int, limit: int) -> None:
        self.client.debug_context_meta = {
            "estimated_tokens": estimated_tokens,
            "limit": limit,
        }

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
        tools = tools + [REACTION_TOOL, READ_COMMENTS_TOOL, SEARCH_BILIBILI_TOOL, SEARCH_XHS_TOOL]
        if include_search:
            tools = tools + [SEARCH_TOOL]
        tools = tools + [_build_speak_tool()]
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
        include_search: bool = True,
    ) -> LLMResponse:
        if not messages:
            return LLMResponse(text="哎，我字呢？")

        tools = (TIMER_TOOLS if include_tools else ALARM_TOOLS) + [REACTION_TOOL, READ_COMMENTS_TOOL, SEARCH_BILIBILI_TOOL, SEARCH_XHS_TOOL]
        if include_search:
            tools = tools + [SEARCH_TOOL]
        tools = tools + [_build_speak_tool()]
        return self.client.stream_with_tools(
            messages=messages,
            system_prompt=load_system_prompt(),
            tools=tools,
            on_text=on_text,
        )
