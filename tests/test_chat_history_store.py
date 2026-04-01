from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.infra.storage.chat_history_store import ChatHistoryStore


class ChatHistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ChatHistoryStore(Path(self._tmp.name))
        self.channel_id = 123

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pop_trailing_entries_by_role_removes_latest_assistant_turn(self) -> None:
        self.store.append_entry(
            channel_id=self.channel_id,
            role="user",
            username="u",
            time="2026-04-01 10:00:00",
            content="hi",
        )
        self.store.append_entry(
            channel_id=self.channel_id,
            role="assistant",
            username="bot",
            time="2026-04-01 10:00:01",
            content="first part",
        )
        self.store.append_entry(
            channel_id=self.channel_id,
            role="assistant",
            username="bot",
            time="2026-04-01 10:00:02",
            content="[IMAGE: cat]",
        )

        removed = self.store.pop_trailing_entries_by_role(
            channel_id=self.channel_id,
            role="assistant",
        )

        self.assertEqual([item["content"] for item in removed], ["[IMAGE: cat]", "first part"])
        remaining = self.store.load_all_entries(channel_id=self.channel_id)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["role"], "user")


if __name__ == "__main__":
    unittest.main()
