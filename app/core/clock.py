"""Timezone-aware clock utility.

Reads the ``TZ`` environment variable (e.g. ``Asia/Shanghai``) so that
``now()`` returns local time even on cloud servers that default to UTC.
"""

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
        return None  # fall back to system local time
    try:
        import zoneinfo
        _tz = zoneinfo.ZoneInfo(tz_name)  # type: ignore[assignment]
    except Exception:
        _tz = None
    return _tz


def now() -> datetime:
    """Return the current datetime in the configured timezone."""
    tz = _load_tz()
    if tz is not None:
        return datetime.now(tz)
    return datetime.now()


def now_clock() -> str:
    """Return current time as ``YYYY-MM-DD HH:MM:SS`` string."""
    return now().strftime("%Y-%m-%d %H:%M:%S")
