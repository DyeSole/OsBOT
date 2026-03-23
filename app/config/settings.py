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
    reset_timer_seconds: float = 2.5
    proactive_idle_seconds: float = 300.0


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
    reset_timer_seconds = float(_env_value("RESET_TIMER_SECONDS", env_file, "2.5").strip() or "2.5")
    proactive_idle_seconds = float(_env_value("PROACTIVE_IDLE_SECONDS", env_file, "300.0").strip() or "300.0")

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
