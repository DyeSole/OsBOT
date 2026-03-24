from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = BASE_DIR / ".env"


@dataclass
class Settings:
    bot_key: str
    discord_bot_token: str
    app_mode: str = "normal"
    base_url: str = ""
    api_key: str = ""
    model: str = "claude-4.6-opus"
    show_error_detail: bool = False
    show_api_payload: bool = False
    show_interaction_logs: bool = True
    session_timeout_seconds: float = 15.0
    typing_detect_delay_seconds: float = 1.0
    reset_timer_seconds: float = 3.0
    proactive_idle_seconds: float = 300.0
    typing_wait: bool = True   # True = wait for typing idle, False = reply immediately
    split_mode: str = "chat"  # "chat" = split by newline, "novel" = no split
    chat_reply_delay_seconds: float = 0.8  # pause between split messages in chat mode
    typing_nudge_seconds: float = 60.0  # seconds before typing/reaction nudge fires
    watch_online_idle_seconds: float = 600.0  # seconds to wait after watched user comes online
    quiet_enabled: bool = False
    quiet_start: str = ""   # e.g. "23:00"
    quiet_end: str = ""     # e.g. "07:00"
    watch_user_ids: list[str] = None  # up to 6 Discord user IDs to monitor presence

    def __post_init__(self) -> None:
        if self.watch_user_ids is None:
            self.watch_user_ids = []


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _env_value(name: str, env_file: dict[str, str], default: str = "") -> str:
    if name in env_file:
        return env_file[name]
    raw = os.getenv(name)
    if raw is not None:
        return raw
    return default


def _env_bool(name: str, env_file: dict[str, str], default: bool) -> bool:
    raw = _env_value(name, env_file)
    if raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    env_file = _read_env_file(ENV_PATH)
    bot_key = _env_value("BOT_KEY", env_file, "Haze").strip() or "Haze"
    mode = _env_value("APP_MODE", env_file, "normal").strip().lower() or "normal"

    base_url = _env_value("BASE_URL", env_file, "").strip()
    api_key = _env_value("API_KEY", env_file, "").strip()
    model = _env_value("MODEL", env_file, "claude-4.6-opus").strip() or "claude-4.6-opus"
    session_timeout_seconds = float(_env_value("SESSION_TIMEOUT_SECONDS", env_file, "15.0").strip() or "15.0")
    typing_detect_delay_seconds = float(
        _env_value("TYPING_DETECT_DELAY_SECONDS", env_file, "1.0").strip() or "1.0"
    )
    reset_timer_seconds = float(_env_value("RESET_TIMER_SECONDS", env_file, "3.0").strip() or "3.0")
    proactive_idle_seconds = float(_env_value("PROACTIVE_IDLE_SECONDS", env_file, "300.0").strip() or "300.0")
    typing_wait = _env_bool("TYPING_WAIT", env_file, True)
    chat_reply_delay_seconds = float(
        _env_value("CHAT_REPLY_DELAY_SECONDS", env_file, "0.8").strip() or "0.8"
    )
    split_mode = _env_value("SPLIT_MODE", env_file, "chat").strip().lower() or "chat"
    if split_mode not in ("chat", "novel"):
        split_mode = "chat"
    typing_nudge_seconds = float(_env_value("TYPING_NUDGE_SECONDS", env_file, "60.0").strip() or "60.0")
    watch_online_idle_seconds = float(_env_value("WATCH_ONLINE_IDLE_SECONDS", env_file, "600.0").strip() or "600.0")
    quiet_enabled = _env_bool("QUIET_ENABLED", env_file, False)
    quiet_start = _env_value("QUIET_START", env_file, "").strip()
    quiet_end = _env_value("QUIET_END", env_file, "").strip()
    raw_watch = _env_value("WATCH_USER_IDS", env_file, "").strip()
    watch_user_ids = [uid.strip() for uid in raw_watch.split(",") if uid.strip()] if raw_watch else []

    return Settings(
        bot_key=bot_key,
        discord_bot_token=_env_value("DISCORD_BOT_TOKEN", env_file, "").strip(),
        app_mode="debug" if mode == "debug" else "normal",
        base_url=base_url,
        api_key=api_key,
        model=model,
        show_error_detail=_env_bool("SHOW_ERROR_DETAIL", env_file, False),
        show_api_payload=_env_bool("SHOW_API_PAYLOAD", env_file, False),
        show_interaction_logs=_env_bool("SHOW_INTERACTION_LOGS", env_file, True),
        session_timeout_seconds=max(1.0, session_timeout_seconds),
        typing_detect_delay_seconds=max(0.0, typing_detect_delay_seconds),
        reset_timer_seconds=max(0.1, reset_timer_seconds),
        proactive_idle_seconds=max(0.0, proactive_idle_seconds),
        typing_nudge_seconds=max(0.0, typing_nudge_seconds),
        watch_online_idle_seconds=max(0.0, watch_online_idle_seconds),
        typing_wait=typing_wait,
        chat_reply_delay_seconds=max(0.0, chat_reply_delay_seconds),
        split_mode=split_mode,
        quiet_enabled=quiet_enabled,
        quiet_start=quiet_start,
        quiet_end=quiet_end,
        watch_user_ids=watch_user_ids,
    )


def env_last_modified() -> float:
    try:
        return ENV_PATH.stat().st_mtime
    except OSError:
        return 0.0


def summarize_settings(settings: Settings) -> dict[str, Any]:
    return {
        "BOT_KEY": settings.bot_key,
        "APP_MODE": settings.app_mode,
        "MODEL": settings.model,
        "BASE_URL": settings.base_url,
        "SHOW_API_PAYLOAD": settings.show_api_payload,
        "SHOW_ERROR_DETAIL": settings.show_error_detail,
        "SHOW_INTERACTION_LOGS": settings.show_interaction_logs,
        "SESSION_TIMEOUT_SECONDS": settings.session_timeout_seconds,
        "TYPING_DETECT_DELAY_SECONDS": settings.typing_detect_delay_seconds,
        "RESET_TIMER_SECONDS": settings.reset_timer_seconds,
        "PROACTIVE_IDLE_SECONDS": settings.proactive_idle_seconds,
        "TYPING_NUDGE_SECONDS": settings.typing_nudge_seconds,
        "WATCH_ONLINE_IDLE_SECONDS": settings.watch_online_idle_seconds,
        "TYPING_WAIT": settings.typing_wait,
        "CHAT_REPLY_DELAY_SECONDS": settings.chat_reply_delay_seconds,
        "SPLIT_MODE": settings.split_mode,
        "QUIET_ENABLED": settings.quiet_enabled,
        "QUIET_START": settings.quiet_start,
        "QUIET_END": settings.quiet_end,
        "WATCH_USER_IDS": settings.watch_user_ids,
        "API_KEY_SET": bool(settings.api_key),
        "DISCORD_BOT_TOKEN_SET": bool(settings.discord_bot_token),
    }


def read_env_values() -> dict[str, str]:
    return _read_env_file(ENV_PATH)


def update_env_values(updates: dict[str, str]) -> None:
    existing_lines: list[str] = []
    if ENV_PATH.exists():
        existing_lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    pending = {key: value for key, value in updates.items() if key}
    result_lines: list[str] = []

    for raw_line in existing_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            result_lines.append(raw_line)
            continue

        key, _value = raw_line.split("=", 1)
        env_key = key.strip()
        if env_key in pending:
            result_lines.append(f"{env_key}={pending.pop(env_key)}")
        else:
            result_lines.append(raw_line)

    for key, value in pending.items():
        result_lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(result_lines).rstrip() + "\n", encoding="utf-8")
