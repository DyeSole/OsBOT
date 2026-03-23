from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


class CompressionStore:
    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def _channel_root(self, channel_id: int) -> Path:
        return self.memory_dir / str(channel_id)

    def _segments_dir(self, channel_id: int) -> Path:
        path = self._channel_root(channel_id) / "segments"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _raw_dir(self, channel_id: int) -> Path:
        path = self._channel_root(channel_id) / "raw"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _index_path(self, channel_id: int) -> Path:
        root = self._channel_root(channel_id)
        root.mkdir(parents=True, exist_ok=True)
        return root / "index.json"

    def save_raw_archive(
        self,
        *,
        channel_id: int,
        source_id: str,
        messages: list[dict[str, str]],
    ) -> Path:
        path = self._raw_dir(channel_id) / f"{self._sanitize_source_id(source_id)}.jsonl"
        self._write_jsonl(path, messages)
        return path

    def save_summary_segment(
        self,
        *,
        channel_id: int,
        source_id: str,
        segment_id: str,
        start_time: str,
        end_time: str,
        message_count: int,
        summary_text: str,
        keywords: list[str],
        generated_at: str,
        source_hash: str,
        version: int = 1,
    ) -> dict[str, Any]:
        segment = {
            "segment_id": segment_id,
            "source_id": source_id,
            "start_time": start_time,
            "end_time": end_time,
            "message_count": message_count,
            "summary_text": summary_text,
            "keywords": keywords,
            "generated_at": generated_at,
            "source_hash": source_hash,
            "version": version,
        }
        path = self._segments_dir(channel_id) / f"{self._sanitize_source_id(source_id)}.json"
        self._write_json(path, segment)
        return segment

    def load_summary_segments(self, *, channel_id: int) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        for path in sorted(self._segments_dir(channel_id).glob("*.json")):
            data = self._load_json_dict(path)
            if data:
                segments.append(data)
        segments.sort(key=lambda item: str(item.get("start_time", "")))
        return segments

    def load_index(self, *, channel_id: int) -> dict[str, Any]:
        return self._load_json_dict(self._index_path(channel_id))

    def update_index(
        self,
        *,
        channel_id: int,
        segment: dict[str, Any],
        version: int = 1,
    ) -> dict[str, Any]:
        index = self.load_index(channel_id=channel_id)
        existing_ids = index.get("segment_ids")
        segment_ids = [str(item) for item in existing_ids] if isinstance(existing_ids, list) else []
        if segment["segment_id"] not in segment_ids:
            segment_ids.append(segment["segment_id"])

        existing_segments = index.get("segments")
        summaries = [item for item in existing_segments if isinstance(item, dict)] if isinstance(existing_segments, list) else []
        meta = self._segment_meta(segment)
        summaries = [item for item in summaries if item.get("segment_id") != segment["segment_id"]]
        summaries.append(meta)
        summaries.sort(key=lambda item: str(item.get("start_time", "")))

        payload = {
            "segment_ids": segment_ids,
            "segments": summaries,
            "version": version,
        }
        self._write_json(self._index_path(channel_id), payload)
        return payload

    @staticmethod
    def build_source_id(*, channel_id: int, start_time: str, end_time: str) -> str:
        return f"{channel_id}:{start_time}:{end_time}"

    @staticmethod
    def build_segment_id() -> str:
        return datetime.now().strftime("seg_%Y%m%d_%H%M%S")

    @staticmethod
    def build_source_hash(messages: list[dict[str, str]]) -> str:
        serialized = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _segment_meta(segment: dict[str, Any]) -> dict[str, Any]:
        return {
            "segment_id": segment.get("segment_id", ""),
            "source_id": segment.get("source_id", ""),
            "start_time": segment.get("start_time", ""),
            "end_time": segment.get("end_time", ""),
            "keywords": segment.get("keywords", []),
        }

    def delete_channel(self, channel_id: int) -> bool:
        """Delete all memory data for a channel. Returns True if data was removed."""
        import shutil
        root = self._channel_root(channel_id)
        if root.exists():
            shutil.rmtree(root)
            return True
        return False

    def all_channel_ids(self) -> set[int]:
        """Return all channel IDs that have memory directories."""
        ids: set[int] = set()
        for path in self.memory_dir.iterdir():
            if path.is_dir():
                try:
                    ids.add(int(path.name))
                except ValueError:
                    pass
        return ids

    @staticmethod
    def _sanitize_source_id(source_id: str) -> str:
        return source_id.replace(":", "_").replace("/", "-").replace("\\", "-")

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    @staticmethod
    def _load_json_dict(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
