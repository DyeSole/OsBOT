from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime


class ChatHistoryStorage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _history_path(self, channel_id: int) -> Path:
        return self.data_dir / f"{channel_id}.jsonl"

    def append_entry(
        self,
        *,
        channel_id: int,
        role: str,
        username: str,
        time: str,
        content: str,
    ) -> None:
        path = self._history_path(channel_id)
        record = {
            "role": role,
            "username": username,
            "time": time,
            "content": content,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load_all_entries(self, *, channel_id: int) -> list[dict[str, str]]:
        path = self._history_path(channel_id)
        if not path.exists():
            return []

        entries: list[dict[str, str]] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            role = row.get("role")
            username = row.get("username")
            timestamp = row.get("time")
            content = row.get("content")
            if not isinstance(role, str) or not isinstance(username, str) or not isinstance(timestamp, str) or not isinstance(content, str):
                continue
            entries.append(
                {
                    "role": role,
                    "username": username,
                    "time": timestamp,
                    "content": content,
                }
            )
        return entries

    @staticmethod
    def _hhmm(timestamp: str) -> str:
        try:
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%H:%M")
        except Exception:
            if " " in timestamp:
                tail = timestamp.split(" ", 1)[1]
                return tail[:5]
            return timestamp[:5]

    def build_transcript_for_api(self, *, channel_id: int) -> str:
        entries = self.load_all_entries(channel_id=channel_id)
        lines: list[str] = []
        for row in entries:
            hhmm = self._hhmm(row["time"])
            username = row["username"]
            content = row["content"].replace("\n", "\\n")
            lines.append(f"[{hhmm} {username}] {content}")
        return "\n".join(lines).strip()
