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


    def build_messages_for_api(
        self,
        *,
        channel_id: int,
        pending_messages: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """Build proper alternating user/assistant messages for the API (enables prefix caching)."""
        from app.infra.storage.chat_history_store import ChatHistoryStore as _S
        summary_block = self._render_summary_block(channel_id=channel_id)
        live_entries = self.history_store.load_all_entries(channel_id=channel_id)
        all_entries = live_entries + [e for e in pending_messages if not e.get("__compressed__")]

        # Group consecutive same-role entries into one message block
        groups: list[tuple[str, list[dict]]] = []
        for entry in all_entries:
            api_role = "assistant" if entry.get("role") == "assistant" else "user"
            if groups and groups[-1][0] == api_role:
                groups[-1][1].append(entry)
            else:
                groups.append((api_role, [entry]))

        messages: list[dict[str, str]] = []
        for api_role, entries in groups:
            text = _S.render_entries(self.history_store, entries)
            if text:
                messages.append({"role": api_role, "content": text})

        # Prepend summary to the first user message (it's stable — only changes on compression)
        if summary_block:
            first_user = next((i for i, m in enumerate(messages) if m["role"] == "user"), None)
            if first_user is not None:
                messages[first_user] = {
                    "role": "user",
                    "content": summary_block + "\n\n" + messages[first_user]["content"],
                }
            else:
                messages.insert(0, {"role": "user", "content": summary_block})

        # Guard: first message must be user
        if messages and messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": ""})

        # Guard: last message must be user (Anthropic API requirement)
        if messages and messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": ""})

        return messages

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
