from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.clock import now as _now


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

    def save_raw_archive(
        self,
        *,
        channel_id: int,
        start_time: str,
        end_time: str,
        messages: list[dict[str, str]],
    ) -> Path:
        filename = self._build_filename(start_time, end_time)
        path = self._raw_dir(channel_id) / f"{filename}.jsonl"
        self._write_jsonl(path, messages)
        return path

    def save_segment(
        self,
        *,
        channel_id: int,
        start_time: str,
        end_time: str,
        summary_text: str,
        keywords: list[str],
    ) -> dict[str, Any]:
        segment = {
            "start_time": start_time,
            "end_time": end_time,
            "summary_text": summary_text,
            "keywords": keywords,
        }
        filename = self._build_filename(start_time, end_time)
        path = self._segments_dir(channel_id) / f"{filename}.json"
        self._write_json(path, segment)
        return segment

    def load_segments(self, *, channel_id: int) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        for path in sorted(self._segments_dir(channel_id).glob("*.json")):
            data = self._load_json_dict(path)
            if data:
                segments.append(data)
        segments.sort(key=lambda item: str(item.get("start_time", "")))
        return segments

    def delete_channel(self, channel_id: int) -> bool:
        import shutil
        root = self._channel_root(channel_id)
        if root.exists():
            shutil.rmtree(root)
            return True
        return False

    def all_channel_ids(self) -> set[int]:
        ids: set[int] = set()
        for path in self.memory_dir.iterdir():
            if path.is_dir():
                try:
                    ids.add(int(path.name))
                except ValueError:
                    pass
        return ids

    @staticmethod
    def _build_filename(start_time: str, end_time: str) -> str:
        return f"{start_time}_{end_time}".replace(":", "-").replace(" ", "_")

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
