from __future__ import annotations

import os
from datetime import datetime, tzinfo

_tz: tzinfo | None = None
_tz_loaded = False


def _load_tz() -> tzinfo | None:
    global _tz, _tz_loaded
    if _tz_loaded:
        return _tz
    _tz_loaded = True
    tz_name = os.environ.get("TZ", "").strip()
    if not tz_name:
        return None
    try:
        import zoneinfo
        _tz = zoneinfo.ZoneInfo(tz_name)  # type: ignore[assignment]
    except Exception:
        _tz = None
    return _tz


def now() -> datetime:
    tz = _load_tz()
    if tz is not None:
        return datetime.now(tz)
    return datetime.now()


def now_clock() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")
