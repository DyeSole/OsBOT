from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = BASE_DIR / ".env"
CONFIG_PATH = BASE_DIR / "data" / "config.json"


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
    typing_wait: bool = True
    split_mode: str = "chat"
    chat_reply_delay_seconds: float = 0.8
    typing_nudge_seconds: float = 60.0
    watch_online_idle_seconds: float = 600.0
    quiet_enabled: bool = False
    quiet_start: str = ""
    quiet_end: str = ""
    watch_user_ids: list[str] = None
    jealousy_channel_ids: list[str] = None
    context_entries: int = 20
    transcript_max_tokens: int = 20000
    search_base_url: str = ""
    search_api_key: str = ""
    search_model: str = ""
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_model: str = ""
    vision_prompt: str = ""
    compression_base_url: str = ""
    compression_api_key: str = ""
    compression_model: str = ""

    def __post_init__(self) -> None:
        if self.watch_user_ids is None:
            self.watch_user_ids = []
        if self.jealousy_channel_ids is None:
            self.jealousy_channel_ids = []


# -- file I/O ----------------------------------------------------------------

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


def _env_value(name: str, merged: dict[str, str], default: str = "") -> str:
    if name in merged:
        return merged[name]
    raw = os.getenv(name)
    if raw is not None:
        return raw
    return default


def _env_bool(name: str, merged: dict[str, str], default: bool) -> bool:
    raw = _env_value(name, merged)
    if raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# -- config.json persistence -------------------------------------------------

def load_config() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(updates: dict[str, str]) -> None:
    config = load_config()
    config.update({k: v for k, v in updates.items() if k})
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# -- load settings ------------------------------------------------------------

def load_settings() -> Settings:
    env_file = _read_env_file(ENV_PATH)
    config = load_config()
    merged = {**env_file, **config}  # config.json overlays .env

    if "TZ" in merged:
        os.environ["TZ"] = merged["TZ"]
    bot_key = _env_value("BOT_KEY", merged, "Haze").strip() or "Haze"
    mode = _env_value("APP_MODE", merged, "normal").strip().lower() or "normal"

    base_url = _env_value("BASE_URL", merged, "").strip()
    api_key = _env_value("API_KEY", merged, "").strip()
    model = _env_value("MODEL", merged, "claude-4.6-opus").strip() or "claude-4.6-opus"
    session_timeout_seconds = float(_env_value("SESSION_TIMEOUT_SECONDS", merged, "15.0").strip() or "15.0")
    typing_detect_delay_seconds = float(
        _env_value("TYPING_DETECT_DELAY_SECONDS", merged, "1.0").strip() or "1.0"
    )
    reset_timer_seconds = float(_env_value("RESET_TIMER_SECONDS", merged, "3.0").strip() or "3.0")
    proactive_idle_seconds = float(_env_value("PROACTIVE_IDLE_SECONDS", merged, "300.0").strip() or "300.0")
    typing_wait = _env_bool("TYPING_WAIT", merged, True)
    chat_reply_delay_seconds = float(
        _env_value("CHAT_REPLY_DELAY_SECONDS", merged, "0.8").strip() or "0.8"
    )
    split_mode = _env_value("SPLIT_MODE", merged, "chat").strip().lower() or "chat"
    if split_mode not in ("chat", "novel"):
        split_mode = "chat"
    typing_nudge_seconds = float(_env_value("TYPING_NUDGE_SECONDS", merged, "60.0").strip() or "60.0")
    watch_online_idle_seconds = float(_env_value("WATCH_ONLINE_IDLE_SECONDS", merged, "600.0").strip() or "600.0")
    quiet_enabled = _env_bool("QUIET_ENABLED", merged, False)
    quiet_start = _env_value("QUIET_START", merged, "").strip()
    quiet_end = _env_value("QUIET_END", merged, "").strip()
    raw_watch = _env_value("WATCH_USER_IDS", merged, "").strip()
    watch_user_ids = [uid.strip() for uid in raw_watch.split(",") if uid.strip()] if raw_watch else []
    raw_jealousy = _env_value("JEALOUSY_CHANNEL_IDS", merged, "").strip()
    jealousy_channel_ids = [cid.strip() for cid in raw_jealousy.split(",") if cid.strip()] if raw_jealousy else []

    context_entries = int(_env_value("CONTEXT_ENTRIES", merged, "20").strip() or "20")
    transcript_max_tokens = int(_env_value("TRANSCRIPT_MAX_TOKENS", merged, "20000").strip() or "20000")

    search_base_url = _env_value("SEARCH_BASE_URL", merged, "").strip()
    search_api_key = _env_value("SEARCH_API_KEY", merged, "").strip()
    search_model = _env_value("SEARCH_MODEL", merged, "").strip()

    vision_base_url = _env_value("VISION_BASE_URL", merged, "").strip()
    vision_api_key = _env_value("VISION_API_KEY", merged, "").strip()
    vision_model = _env_value("VISION_MODEL", merged, "").strip()
    vision_prompt = _env_value("VISION_PROMPT", merged, "").strip()

    compression_base_url = _env_value("COMPRESSION_BASE_URL", merged, "").strip()
    compression_api_key = _env_value("COMPRESSION_API_KEY", merged, "").strip()
    compression_model = _env_value("COMPRESSION_MODEL", merged, "").strip()

    return Settings(
        bot_key=bot_key,
        discord_bot_token=_env_value("DISCORD_BOT_TOKEN", merged, "").strip(),
        app_mode="debug" if mode == "debug" else "normal",
        base_url=base_url,
        api_key=api_key,
        model=model,
        show_error_detail=_env_bool("SHOW_ERROR_DETAIL", merged, False),
        show_api_payload=_env_bool("SHOW_API_PAYLOAD", merged, False),
        show_interaction_logs=_env_bool("SHOW_INTERACTION_LOGS", merged, True),
        session_timeout_seconds=max(1.0, session_timeout_seconds),
        typing_detect_delay_seconds=max(0.0, typing_detect_delay_seconds),
        reset_timer_seconds=max(0.1, reset_timer_seconds),
        proactive_idle_seconds=max(0.0, proactive_idle_seconds),
        typing_nudge_seconds=max(0.0, typing_nudge_seconds),
        watch_online_idle_seconds=max(0.0, watch_online_idle_seconds),
        context_entries=max(1, context_entries),
        transcript_max_tokens=max(1000, transcript_max_tokens),
        typing_wait=typing_wait,
        chat_reply_delay_seconds=max(0.0, chat_reply_delay_seconds),
        split_mode=split_mode,
        quiet_enabled=quiet_enabled,
        quiet_start=quiet_start,
        quiet_end=quiet_end,
        watch_user_ids=watch_user_ids,
        jealousy_channel_ids=jealousy_channel_ids,
        search_base_url=search_base_url,
        search_api_key=search_api_key,
        search_model=search_model,
        vision_base_url=vision_base_url,
        vision_api_key=vision_api_key,
        vision_model=vision_model,
        vision_prompt=vision_prompt,
        compression_base_url=compression_base_url,
        compression_api_key=compression_api_key,
        compression_model=compression_model,
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
        "CONTEXT_ENTRIES": settings.context_entries,
        "TRANSCRIPT_MAX_TOKENS": settings.transcript_max_tokens,
        "WATCH_ONLINE_IDLE_SECONDS": settings.watch_online_idle_seconds,
        "TYPING_WAIT": settings.typing_wait,
        "CHAT_REPLY_DELAY_SECONDS": settings.chat_reply_delay_seconds,
        "SPLIT_MODE": settings.split_mode,
        "QUIET_ENABLED": settings.quiet_enabled,
        "QUIET_START": settings.quiet_start,
        "QUIET_END": settings.quiet_end,
        "WATCH_USER_IDS": settings.watch_user_ids,
        "JEALOUSY_CHANNEL_IDS": settings.jealousy_channel_ids,
        "API_KEY_SET": bool(settings.api_key),
        "DISCORD_BOT_TOKEN_SET": bool(settings.discord_bot_token),
        "SEARCH_BASE_URL": settings.search_base_url,
        "SEARCH_MODEL": settings.search_model,
        "SEARCH_API_KEY_SET": bool(settings.search_api_key),
        "VISION_BASE_URL": settings.vision_base_url,
        "VISION_MODEL": settings.vision_model,
        "VISION_API_KEY_SET": bool(settings.vision_api_key),
    }
