from __future__ import annotations

from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

PROMPT_TARGETS = {
    "soul": PROMPTS_DIR / "soul.txt",
    "userinfo": PROMPTS_DIR / "userinfo.txt",
    "compression": PROMPTS_DIR / "compression.txt",
    "proactive": PROMPTS_DIR / "proactive.txt",
    "morning": PROMPTS_DIR / "morning.txt",
    "watch_online": PROMPTS_DIR / "watch_online.txt",
    "jealousy": PROMPTS_DIR / "jealousy.txt",
    "typing_nudge": PROMPTS_DIR / "typing_nudge.txt",
    "vision_describe": PROMPTS_DIR / "vision_describe.txt",
    "link_summary": PROMPTS_DIR / "link_summary.txt",
}


class PromptService:
    def read_prompt(self, target: str) -> str:
        path = self._path_for(target)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def write_prompt(self, *, target: str, content: str) -> Path:
        path = self._path_for(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        normalized = content.rstrip() + "\n"
        path.write_text(normalized, encoding="utf-8")
        return path

    @staticmethod
    def _path_for(target: str) -> Path:
        if target not in PROMPT_TARGETS:
            raise ValueError(f"unsupported prompt target: {target}")
        return PROMPT_TARGETS[target]
