from __future__ import annotations

from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

PROMPT_TARGETS = {
    "soul": PROMPTS_DIR / "soul.txt",
    "userinfo": PROMPTS_DIR / "userinfo.txt",
    "compression": PROMPTS_DIR / "compression.txt",
    "pixai": PROMPTS_DIR / "pixai.txt",
    "proactive": PROMPTS_DIR / "proactive.txt",
    "morning": PROMPTS_DIR / "morning.txt",
    "watch_online": PROMPTS_DIR / "watch_online.txt",
    "vision": PROMPTS_DIR / "vision.txt",
    "novel": PROMPTS_DIR / "novel.txt",
    "jealousy": PROMPTS_DIR / "jealousy.txt",
    "jealousy_quiet": PROMPTS_DIR / "jealousy_quiet.txt",
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
