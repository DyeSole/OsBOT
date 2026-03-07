from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


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
    reset_timer_seconds: float = 2.5


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    _load_env_file(ENV_PATH)
    bot_key = os.getenv("BOT_KEY", "Haze").strip() or "Haze"
    mode = os.getenv("APP_MODE", "normal").strip().lower() or "normal"

    base_url = os.getenv("BASE_URL", "").strip()
    api_key = os.getenv("API_KEY", "").strip()
    model = os.getenv("MODEL", "claude-4.6-opus").strip() or "claude-4.6-opus"
    session_timeout_seconds = float(os.getenv("SESSION_TIMEOUT_SECONDS", "15.0").strip() or "15.0")
    typing_detect_delay_seconds = float(
        os.getenv("TYPING_DETECT_DELAY_SECONDS", "1.0").strip() or "1.0"
    )
    reset_timer_seconds = float(os.getenv("RESET_TIMER_SECONDS", "2.5").strip() or "2.5")

    return Settings(
        bot_key=bot_key,
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", "").strip(),
        app_mode="debug" if mode == "debug" else "normal",
        base_url=base_url,
        api_key=api_key,
        model=model,
        show_error_detail=_env_bool("SHOW_ERROR_DETAIL", False),
        show_api_payload=_env_bool("SHOW_API_PAYLOAD", False),
        show_interaction_logs=_env_bool("SHOW_INTERACTION_LOGS", True),
        session_timeout_seconds=max(1.0, session_timeout_seconds),
        typing_detect_delay_seconds=max(0.0, typing_detect_delay_seconds),
        reset_timer_seconds=max(0.1, reset_timer_seconds),
    )
