from __future__ import annotations

from app.infra.storage import ChatHistoryStore, CompressionStore


class ContextBuilder:
    def __init__(self, history_store: ChatHistoryStore, compression_store: CompressionStore):
        self.history_store = history_store
        self.compression_store = compression_store

    def build_context_for_api(
        self,
        *,
        channel_id: int,
        pending_messages: list[dict[str, str]],
    ) -> str:
        summary_block = self._render_summary_block(channel_id=channel_id)
        live_block = self.history_store.render_entries(
            self.history_store.load_all_entries(channel_id=channel_id)
        )
        pending_block = self.history_store.render_entries(pending_messages)

        return "\n\n".join(
            block for block in [summary_block, live_block, pending_block] if block
        ).strip()

    def build_messages_for_api(
        self,
        *,
        channel_id: int,
        pending_messages: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], str]:
        """Return (messages, summary) for the LLM API.

        messages: alternating user/assistant entries + pending_messages appended.
        summary: rendered summary text to be injected into system prompt.
        """
        summary = self._render_summary_block(channel_id=channel_id)
        entries = self.history_store.load_all_entries(channel_id=channel_id)
        messages = self.history_store.entries_to_messages(entries + pending_messages)
        return messages, summary

    def build_live_block(self, *, channel_id: int) -> str:
        return self.history_store.render_entries(
            self.history_store.load_all_entries(channel_id=channel_id)
        )

    @staticmethod
    def estimate_tokens(text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)

    def _render_summary_block(self, *, channel_id: int) -> str:
        segments = self.compression_store.load_summary_segments(channel_id=channel_id)
        if not segments:
            return ""

        lines = ["[历史摘要]"]
        for segment in segments:
            start_time = str(segment.get("start_time", ""))
            end_time = str(segment.get("end_time", ""))
            summary_text = str(segment.get("summary_text", "")).strip()
            if not summary_text:
                continue
            lines.append(f"[{start_time} ~ {end_time}] {summary_text}")
        return "\n".join(lines).strip() if len(lines) > 1 else ""
