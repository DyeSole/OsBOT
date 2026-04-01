from __future__ import annotations

from pathlib import Path
import re


PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
USER_PROMPT_PATH = PROMPTS_DIR / "user.txt"
OTHER_PROMPTS_PATH = PROMPTS_DIR / "others.txt"
OTHER_PROMPT_TITLES = {
    "compression": "压缩提示词",
    "pixai": "画图提示词",
    "proactive": "主动消息提示词",
    "morning": "早安提示词",
    "watch_online": "上线监听提示词",
    "vision": "识图提示词",
    "jealousy": "吃醋提示词",
    "jealousy_quiet": "安静时间吃醋提示词",
    "typing_nudge_note": "打字提醒提示词",
    "timer_expired_note": "计时器到期提示词",
    "expired_alarm_list_note": "闹钟汇总提示词",
    "pending_reaction_list_note": "表情反应汇总提示词",
    "alarm_due_note": "单个闹钟到期提示词",
    "quiet_end_note": "静默结束提示词",
}

PROMPT_TARGETS = {
    "soul": PROMPTS_DIR / "soul.txt",
    "userinfo": USER_PROMPT_PATH,
    "novel": PROMPTS_DIR / "novel.txt",
}


class PromptService:
    def read_prompt(self, target: str) -> str:
        if target in OTHER_PROMPT_TITLES:
            other_prompts = self._read_other_prompts()
            if target in other_prompts:
                return other_prompts[target]
        path = self._path_for(target)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def write_prompt(self, *, target: str, content: str) -> Path:
        if target in OTHER_PROMPT_TITLES:
            other_prompts = self._read_other_prompts()
            other_prompts[target] = self._normalize(content)
            self._write_other_prompts(other_prompts)
            return OTHER_PROMPTS_PATH
        path = self._path_for(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._normalize(content), encoding="utf-8")
        return path

    @staticmethod
    def _path_for(target: str) -> Path:
        if target not in PROMPT_TARGETS:
            raise ValueError(f"unsupported prompt target: {target}")
        return PROMPT_TARGETS[target]

    @staticmethod
    def _normalize(content: str) -> str:
        return content.rstrip() + "\n"

    def _read_other_prompts(self) -> dict[str, str]:
        text = ""
        if OTHER_PROMPTS_PATH.exists():
            try:
                text = OTHER_PROMPTS_PATH.read_text(encoding="utf-8")
            except OSError:
                text = ""
        if not text:
            return self._build_other_prompt_defaults()

        prompts: dict[str, str] = {}
        current_target: str | None = None
        current_lines: list[str] = []
        header_re = re.compile(r"^【(.+)】\s*$")
        title_to_target = {title: target for target, title in OTHER_PROMPT_TITLES.items()}

        def flush() -> None:
            nonlocal current_target, current_lines
            if current_target is None:
                return
            prompts[current_target] = self._normalize("\n".join(current_lines)).rstrip("\n") + "\n"
            current_target = None
            current_lines = []

        for line in text.splitlines():
            match = header_re.match(line.strip())
            if match:
                flush()
                current_target = title_to_target.get(match.group(1).strip())
                current_lines = []
                continue
            if current_target is not None:
                current_lines.append(line)
        flush()

        defaults = self._build_other_prompt_defaults()
        for target, content in defaults.items():
            prompts.setdefault(target, content)
        return prompts

    def _write_other_prompts(self, prompts: dict[str, str]) -> None:
        OTHER_PROMPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        blocks: list[str] = []
        for target, title in OTHER_PROMPT_TITLES.items():
            body = prompts.get(target, "")
            blocks.append(f"【{title}】")
            blocks.append(body.rstrip())
            blocks.append("")
        OTHER_PROMPTS_PATH.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")

    def _build_other_prompt_defaults(self) -> dict[str, str]:
        prompts: dict[str, str] = {}
        legacy_paths = {
            "compression": PROMPTS_DIR / "compression.txt",
            "pixai": PROMPTS_DIR / "pixai.txt",
            "proactive": PROMPTS_DIR / "proactive.txt",
            "morning": PROMPTS_DIR / "morning.txt",
            "watch_online": PROMPTS_DIR / "watch_online.txt",
            "vision": PROMPTS_DIR / "vision.txt",
            "jealousy": PROMPTS_DIR / "jealousy.txt",
            "jealousy_quiet": PROMPTS_DIR / "jealousy_quiet.txt",
            "typing_nudge_note": PROMPTS_DIR / "typing_nudge_note.txt",
            "timer_expired_note": PROMPTS_DIR / "timer_expired_note.txt",
            "expired_alarm_list_note": PROMPTS_DIR / "expired_alarm_list_note.txt",
            "pending_reaction_list_note": PROMPTS_DIR / "pending_reaction_list_note.txt",
            "alarm_due_note": PROMPTS_DIR / "alarm_due_note.txt",
            "quiet_end_note": PROMPTS_DIR / "quiet_end_note.txt",
        }
        for target in OTHER_PROMPT_TITLES:
            path = legacy_paths[target]
            try:
                prompts[target] = path.read_text(encoding="utf-8")
            except OSError:
                prompts[target] = ""
        return prompts
