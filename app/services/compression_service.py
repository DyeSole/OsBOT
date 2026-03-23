from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config.settings import Settings
from app.infra.llm_client import LLMClient
from app.infra.storage import ChatHistoryStore, CompressionStore


PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
COMPRESSION_PROMPT_PATH = PROMPTS_DIR / "compression.txt"
DEFAULT_COMPRESSION_PROMPT = (
    "请输出 JSON，包含 summary_text 和 keywords 两个字段。"
)


def load_compression_prompt() -> str:
    try:
        return COMPRESSION_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_COMPRESSION_PROMPT


class CompressionService:
    def __init__(
        self,
        settings: Settings,
        history_store: ChatHistoryStore,
        compression_store: CompressionStore,
    ):
        self.history_store = history_store
        self.compression_store = compression_store
        self.apply_settings(settings)

    def apply_settings(self, settings: Settings) -> None:
        self.client = LLMClient(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
        )

    def compress_history(self, *, channel_id: int) -> dict[str, Any] | None:
        messages = self.history_store.load_entries_after_marker(channel_id=channel_id)
        if not messages:
            return None

        transcript = self.history_store.render_entries(messages)
        llm_result = self._generate_summary_result(transcript)

        start_time = messages[0]["time"]
        end_time = messages[-1]["time"]
        segment_id = self.compression_store.build_segment_id(start_time)

        self.compression_store.save_raw_archive(
            channel_id=channel_id,
            segment_id=segment_id,
            messages=messages,
        )
        segment = self.compression_store.save_summary_segment(
            channel_id=channel_id,
            segment_id=segment_id,
            start_time=start_time,
            end_time=end_time,
            message_count=len(messages),
            summary_text=str(llm_result.get("summary_text", "")).strip(),
            keywords=self._normalize_keywords(llm_result.get("keywords")),
        )
        self.compression_store.update_index(
            channel_id=channel_id,
            segment=segment,
        )
        self.history_store.reset_active_history(channel_id=channel_id)
        return segment

    def _generate_summary_result(self, transcript: str) -> dict[str, Any]:
        messages = [{"role": "user", "content": transcript}]
        raw = self.client.generate(messages=messages, system_prompt=load_compression_prompt())
        data = self._parse_json_object(raw)

        summary_text = str(data.get("summary_text", "")).strip()
        keywords = self._normalize_keywords(data.get("keywords"))

        if summary_text:
            return {
                "summary_text": summary_text,
                "keywords": keywords,
            }

        fallback = transcript[:600].strip()
        if not fallback:
            fallback = "无可用摘要。"
        return {
            "summary_text": fallback,
            "keywords": keywords,
        }

    @staticmethod
    def _normalize_keywords(raw_keywords: Any) -> list[str]:
        if not isinstance(raw_keywords, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw_keywords:
            text = str(item).strip()
            if not text:
                continue
            lowered = text.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            out.append(text)
        return out

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        text = (raw or "").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except Exception:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            snippet = text[start : end + 1]
            try:
                data = json.loads(snippet)
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}
