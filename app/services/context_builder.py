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
        anchor_block = self._render_anchor_block(channel_id=channel_id)
        live_block = self.history_store.render_entries(
            self.history_store.load_all_entries(channel_id=channel_id)
        )
        pending_block = self.history_store.render_entries(pending_messages)

        return "\n\n".join(
            block for block in [summary_block, anchor_block, live_block, pending_block] if block
        ).strip()

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

    def _render_anchor_block(self, *, channel_id: int) -> str:
        anchor_rows = self.compression_store.load_anchor_window(channel_id=channel_id, limit=30)
        if not anchor_rows:
            return ""
        rendered = self.history_store.render_entries(anchor_rows)
        return f"[锚点前30条]\n{rendered}".strip()
