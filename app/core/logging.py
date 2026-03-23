from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.core.clock import now_clock


ERROR_EMOJI = {
    "CONFIG": "🔑",
    "NETWORK": "🌐",
    "API": "🤖",
    "STORAGE": "💾",
    "LOGIC": "🧩",
    "UNKNOWN": "🚨",
}


@dataclass
class BotLogger:
    bot_key: str
    mode: str = "normal"
    show_error_detail: bool = False

    def _ts(self) -> str:
        return now_clock()

    def info(self, message: str) -> None:
        print(f"{self._ts()} | ℹ️ INFO | {self.bot_key} | {message}")

    def startup_jar(self, cat_count: int) -> None:
        print(
            f"{self._ts()} | 🧸 STARTUP | {self.bot_key} | "
            f"姗宝的罐子打开啦，里面有 {cat_count} 只猫。"
        )

    def error(
        self,
        error_type: str,
        message: str,
        *,
        chat_id: Optional[int] = None,
        exc: Optional[Exception] = None,
    ) -> None:
        typ = error_type if error_type in ERROR_EMOJI else "UNKNOWN"
        if typ == "UNKNOWN" and not self.show_error_detail:
            return
        emoji = ERROR_EMOJI[typ]
        cid = str(chat_id) if chat_id is not None else "-"
        print(f"{self._ts()} | {emoji} {typ} | {self.bot_key} | {cid} | {message}")

        if self.mode.lower() == "debug" and self.show_error_detail and exc is not None:
            etype = exc.__class__.__name__
            print(f"{self._ts()} | 🔍 DEBUG | {self.bot_key} | {etype} | {exc}")
