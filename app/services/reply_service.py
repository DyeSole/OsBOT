from __future__ import annotations

from pathlib import Path

from app.config.settings import Settings
from app.infra.llm_client import LLMClient


PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
SOUL_PROMPT_PATH = PROMPTS_DIR / "soul.txt"


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
