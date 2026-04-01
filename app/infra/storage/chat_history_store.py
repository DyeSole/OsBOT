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
        return self._read_jsonl_entries(self._history_path(channel_id))

    def load_entries_after_marker(self, *, channel_id: int) -> list[dict[str, str]]:
        rows = self._read_jsonl_rows(self._history_path(channel_id))
        last_marker = -1
        for i, row in enumerate(rows):
            if row.get("__compressed__"):
                last_marker = i

        entries: list[dict[str, str]] = []
        for row in rows[last_marker + 1:]:
            if row.get("__compressed__"):
                continue
            entry = self._parse_entry(row)
            if entry:
                entries.append(entry)
        return entries

    def reset_active_history(self, *, channel_id: int, keep: int = 30) -> None:
        path = self._history_path(channel_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = self._read_jsonl_entries(path)
        tail = entries[-keep:] if entries else []
        lines: list[str] = []
        for entry in tail:
            lines.append(json.dumps(entry, ensure_ascii=False))
        lines.append(json.dumps(COMPRESSION_MARKER, ensure_ascii=False))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def entries_to_messages(self, entries: list[dict[str, str]]) -> list[dict[str, str]]:
        """Convert entries to alternating user/assistant messages for the API.

        - user/system entries  → role="user",  content="[HH:MM username] ..."
        - assistant entries    → role="assistant", content="[HH:MM] ..."
        - Consecutive same-role entries are merged with newline.
        """
        if not entries:
            return []
        messages: list[dict[str, str]] = []
        for row in entries:
            hhmm = self._hhmm(row["time"])
            role = row["role"]
            content = row["content"]
            if role == "assistant":
                api_role = "assistant"
                line = f"[{hhmm}] {content}"
            else:
                api_role = "user"
                line = f"[{hhmm} {row['username']}] {content}"
            if messages and messages[-1]["role"] == api_role:
                messages[-1]["content"] += "\n" + line
            else:
                messages.append({"role": api_role, "content": line})
        return messages

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
    def _read_jsonl_rows(path: Path) -> list[dict]:
        """Read a JSONL file and return all parsed dicts (skipping blanks and bad lines)."""
        if not path.exists():
            return []
        rows: list[dict] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    @classmethod
    def _read_jsonl_entries(cls, path: Path) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for row in cls._read_jsonl_rows(path):
            if row.get("__compressed__"):
                continue
            entry = cls._parse_entry(row)
            if entry:
                entries.append(entry)
        return entries

    @staticmethod
    def _find_last_line_by_role(lines: list[str], role: str) -> tuple[int, dict | None]:
        """Return (index, parsed_row) of the last line matching role, or (-1, None)."""
        for i in range(len(lines) - 1, -1, -1):
            raw = lines[i].strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if isinstance(row, dict) and row.get("role") == role:
                return i, row
        return -1, None

    def pop_last_by_role(self, *, channel_id: int, role: str) -> dict[str, str] | None:
        path = self._history_path(channel_id)
        if not path.exists():
            return None
        lines = path.read_text(encoding="utf-8").splitlines()
        idx, entry = self._find_last_line_by_role(lines, role)
        if idx < 0:
            return None
        del lines[idx]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return entry

    def replace_last_by_role(
        self, *, channel_id: int, role: str, new_content: str,
    ) -> bool:
        """Replace the content of the last entry with the given role. Returns True on success."""
        path = self._history_path(channel_id)
        if not path.exists():
            return False
        lines = path.read_text(encoding="utf-8").splitlines()
        idx, row = self._find_last_line_by_role(lines, role)
        if idx < 0:
            return False
        row["content"] = new_content
        lines[idx] = json.dumps(row, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True

    def pop_trailing_entries_by_role(
        self,
        *,
        channel_id: int,
        role: str,
    ) -> list[dict[str, str]]:
        """Pop the contiguous trailing entries with the given role."""
        path = self._history_path(channel_id)
        if not path.exists():
            return []

        lines = path.read_text(encoding="utf-8").splitlines()
        removed: list[dict[str, str]] = []

        while lines:
            raw = lines[-1].strip()
            if not raw:
                lines.pop()
                continue
            try:
                row = json.loads(raw)
            except Exception:
                break
            if not isinstance(row, dict) or row.get("role") != role:
                break
            entry = self._parse_entry(row)
            if entry:
                removed.append(entry)
            lines.pop()

        if removed:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return removed

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
