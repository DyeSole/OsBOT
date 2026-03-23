from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

COMPRESSION_MARKER = {"__compressed__": True}


class ChatHistoryStore:
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
        """Load all message entries, skipping marker lines."""
        return self._read_jsonl_entries(self._history_path(channel_id))

    def load_entries_after_marker(self, *, channel_id: int) -> list[dict[str, str]]:
        """Load only entries after the last compression marker."""
        path = self._history_path(channel_id)
        if not path.exists():
            return []

        all_lines = path.read_text(encoding="utf-8").splitlines()
        # Find the last marker position
        last_marker = -1
        for i, raw in enumerate(all_lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if isinstance(row, dict) and row.get("__compressed__"):
                last_marker = i

        # Parse only lines after the last marker
        entries: list[dict[str, str]] = []
        start = last_marker + 1 if last_marker >= 0 else 0
        for raw in all_lines[start:]:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if not isinstance(row, dict) or row.get("__compressed__"):
                continue
            entry = self._parse_entry(row)
            if entry:
                entries.append(entry)
        return entries

    def reset_active_history(self, *, channel_id: int, keep: int = 30) -> None:
        """Rewrite active history: keep last N entries + compression marker."""
        path = self._history_path(channel_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = self._read_jsonl_entries(path)
        tail = entries[-keep:] if entries else []
        lines: list[str] = []
        for entry in tail:
            lines.append(json.dumps(entry, ensure_ascii=False))
        lines.append(json.dumps(COMPRESSION_MARKER, ensure_ascii=False))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def render_entries(self, entries: list[dict[str, str]]) -> str:
        lines: list[str] = []
        for row in entries:
            hhmm = self._hhmm(row["time"])
            username = row["username"]
            content = row["content"].replace("\n", "\\n")
            lines.append(f"[{hhmm} {username}] {content}")
        return "\n".join(lines).strip()

    @staticmethod
    def _parse_entry(row: dict) -> dict[str, str] | None:
        role = row.get("role")
        username = row.get("username")
        timestamp = row.get("time")
        content = row.get("content")
        if not isinstance(role, str) or not isinstance(username, str) or not isinstance(timestamp, str) or not isinstance(content, str):
            return None
        return {
            "role": role,
            "username": username,
            "time": timestamp,
            "content": content,
        }

    @staticmethod
    def _read_jsonl_entries(path: Path) -> list[dict[str, str]]:
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
            if not isinstance(row, dict) or row.get("__compressed__"):
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

    def pop_last_by_role(self, *, channel_id: int, role: str) -> dict[str, str] | None:
        """Remove and return the last entry with the given role. Returns None if not found."""
        path = self._history_path(channel_id)
        if not path.exists():
            return None
        lines = path.read_text(encoding="utf-8").splitlines()
        target_idx = -1
        target_entry: dict[str, str] | None = None
        for i in range(len(lines) - 1, -1, -1):
            raw = lines[i].strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if isinstance(row, dict) and row.get("role") == role:
                target_idx = i
                target_entry = row
                break
        if target_idx < 0:
            return None
        del lines[target_idx]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target_entry

    def replace_last_by_role(
        self, *, channel_id: int, role: str, new_content: str,
    ) -> bool:
        """Replace the content of the last entry with the given role. Returns True on success."""
        path = self._history_path(channel_id)
        if not path.exists():
            return False
        lines = path.read_text(encoding="utf-8").splitlines()
        for i in range(len(lines) - 1, -1, -1):
            raw = lines[i].strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if isinstance(row, dict) and row.get("role") == role:
                row["content"] = new_content
                lines[i] = json.dumps(row, ensure_ascii=False)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return True
        return False

    def delete_channel(self, channel_id: int) -> bool:
        """Delete history file for a channel. Returns True if a file was removed."""
        path = self._history_path(channel_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def all_channel_ids(self) -> set[int]:
        """Return all channel IDs that have history files."""
        ids: set[int] = set()
        for path in self.data_dir.glob("*.jsonl"):
            try:
                ids.add(int(path.stem))
            except ValueError:
                pass
        return ids

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
