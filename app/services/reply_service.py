from __future__ import annotations

from app.config.settings import Settings
from app.infra.llm_client import LLMClient


SYSTEM_PROMPT = (
    "你是Haze，Dee的老公，冷幽默与温柔浪漫并存，没事儿就耍贱，喜欢骚扰Dee。"
)


class ReplyService:
    def __init__(self, settings: Settings):
        self.client = LLMClient(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
        )

    def generate_reply(self, messages: list[dict[str, str]]) -> str:
        if not messages:
            return "哎，我字呢？"

        return self.client.generate(messages=messages, system_prompt=SYSTEM_PROMPT)
