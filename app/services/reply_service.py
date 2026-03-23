from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.config.settings import Settings
from app.infra.llm_client import LLMClient, LLMResponse


PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
SOUL_PROMPT_PATH = PROMPTS_DIR / "soul.txt"

TOOLS: list[dict[str, Any]] = [
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


def load_system_prompt() -> str:
    try:
        return SOUL_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class ReplyService:
    def __init__(self, settings: Settings):
        self.apply_settings(settings)

    def apply_settings(self, settings: Settings) -> None:
        self.client = LLMClient(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
        )

    def generate_reply(self, messages: list[dict[str, str]]) -> str:
        if not messages:
            return "哎，我字呢？"

        return self.client.generate(messages=messages, system_prompt=load_system_prompt())

    def generate_reply_with_tools(self, messages: list[dict[str, str]]) -> LLMResponse:
        if not messages:
            return LLMResponse(text="哎，我字呢？")

        return self.client.generate_with_tools(
            messages=messages,
            system_prompt=load_system_prompt(),
            tools=TOOLS,
        )

    def stream_reply_with_tools(
        self,
        messages: list[dict[str, str]],
        on_text: Callable[[str], None],
    ) -> LLMResponse:
        if not messages:
            return LLMResponse(text="哎，我字呢？")

        return self.client.stream_with_tools(
            messages=messages,
            system_prompt=load_system_prompt(),
            tools=TOOLS,
            on_text=on_text,
        )
