from __future__ import annotations

import json
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
        segment_id: str,
        messages: list[dict[str, str]],
    ) -> Path:
        path = self._raw_dir(channel_id) / f"{segment_id}.jsonl"
        self._write_jsonl(path, messages)
        return path

    def save_summary_segment(
        self,
        *,
        channel_id: int,
        segment_id: str,
        start_time: str,
        end_time: str,
        message_count: int,
        summary_text: str,
        keywords: list[str],
    ) -> dict[str, Any]:
        segment = {
            "id": segment_id,
            "start_time": start_time,
            "end_time": end_time,
            "message_count": message_count,
            "summary_text": summary_text,
            "keywords": keywords,
        }
        path = self._segments_dir(channel_id) / f"{segment_id}.json"
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

    def load_index(self, *, channel_id: int) -> list[dict[str, Any]]:
        return self._load_json_list(self._index_path(channel_id))

    def update_index(
        self,
        *,
        channel_id: int,
        segment: dict[str, Any],
    ) -> list[dict[str, Any]]:
        index = self.load_index(channel_id=channel_id)
        seg_id = segment.get("id", "")

        # 去重：移除已有的同 id 条目
        index = [item for item in index if item.get("id") != seg_id]
        index.append({
            "id": seg_id,
            "start_time": segment.get("start_time", ""),
            "end_time": segment.get("end_time", ""),
            "keywords": segment.get("keywords", []),
        })
        index.sort(key=lambda item: str(item.get("start_time", "")))

        self._write_json(self._index_path(channel_id), index)
        return index

    @staticmethod
    def build_segment_id(start_time: str) -> str:
        """从 start_time (如 '2026-03-11 20:00:00') 生成 seg_YYYYmmdd_HHmmss。"""
        cleaned = start_time.replace("-", "").replace(":", "").replace(" ", "_")
        return f"seg_{cleaned}"

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
    def _write_json(path: Path, payload: Any) -> None:
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

    @staticmethod
    def _load_json_list(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return data if isinstance(data, list) else []
