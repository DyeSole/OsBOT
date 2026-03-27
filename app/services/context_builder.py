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

    def build_live_block(self, *, channel_id: int) -> str:
        return self.history_store.render_entries(
            self.history_store.load_all_entries(channel_id=channel_id)
        )

    @staticmethod
    def estimate_tokens(text: str) -> int:
        if not text:
            return 0
        # Chinese chars ~1.5 tokens each, ASCII/space ~0.25 tokens each
        cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf")
        other = len(text) - cjk
        return max(1, int(cjk * 1.5 + other * 0.25))

    def _render_summary_block(self, *, channel_id: int) -> str:
        segments = self.compression_store.load_segments(channel_id=channel_id)
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
