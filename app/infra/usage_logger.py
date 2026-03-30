from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.clock import now_clock

log = logging.getLogger(__name__)

USAGE_DIR = Path(__file__).resolve().parents[2] / "data" / "usage"
USAGE_FILE = USAGE_DIR / "usage.jsonl"


def log_usage(
    *,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    source: str = "",
) -> None:
    if input_tokens is None and output_tokens is None:
        return
    try:
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": now_clock(),
            "model": model,
            "in": input_tokens,
            "out": output_tokens,
        }
        if source:
            record["src"] = source
        with USAGE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        log.debug("failed to write usage log", exc_info=True)
