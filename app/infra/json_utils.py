"""Extract JSON structures from LLM text that may contain markdown or filler."""
from __future__ import annotations

import json
import re


def extract_json_object(text: str) -> dict:
    """Extract the first JSON object ({...}) from *text*. Returns {} on failure."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def extract_json_array(text: str) -> list:
    """Extract the first JSON array ([...]) from *text*. Returns [] on failure."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []
