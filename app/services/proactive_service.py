"""Proactive engagement service.

Responsibilities:
- Schedule idle timers per channel after bot replies.
- Reset timers when users send messages.
- Fire proactive API requests when idle threshold is reached.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.reply_service import ReplyService
    from app.services.context_builder import ContextBuilder
    from app.infra.storage import ChatHistoryStore

PROACTIVE_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "proactive.txt"


def _load_proactive_prompt() -> str:
    try:
        return PROACTIVE_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "用户已经有一段时间没有跟你说话了，你要主动跟他说点什么吗？"


class ProactiveService:
    """Manages per-channel idle timers that trigger proactive bot messages."""

    def __init__(
        self,
        *,
        idle_seconds: float,
        reply_service: ReplyService,
        context_builder: ContextBuilder,
        history_store: ChatHistoryStore,
    ):
        self.idle_seconds = idle_seconds
        self.reply_service = reply_service
        self.context_builder = context_builder
        self.history_store = history_store
        # channel_id -> running asyncio.Task
        self._timers: dict[int, asyncio.Task] = {}

    def update_idle_seconds(self, value: float) -> None:
        self.idle_seconds = value

    def schedule(self, channel_id: int, send_callback) -> None:
        """Start or restart the idle timer for a channel.

        Args:
            channel_id: The Discord channel ID.
            send_callback: An async callable(reply: str) that sends
                           the proactive message to the channel.
        """
        if self.idle_seconds <= 0:
            return
        self.cancel(channel_id)
        self._timers[channel_id] = asyncio.create_task(
            self._wait_and_fire(channel_id, send_callback)
        )

    def cancel(self, channel_id: int) -> None:
        """Cancel the idle timer for a channel (e.g. when a user sends a message)."""
        task = self._timers.pop(channel_id, None)
        if task is not None and not task.done():
            task.cancel()

    def cancel_all(self) -> None:
        for channel_id in list(self._timers):
            self.cancel(channel_id)

    async def _wait_and_fire(self, channel_id: int, send_callback) -> None:
        """Sleep for idle_seconds, then ask the AI if it wants to speak."""
        await asyncio.sleep(self.idle_seconds)
        self._timers.pop(channel_id, None)

        proactive_prompt = _load_proactive_prompt()

        # Build context so the AI can see the recent conversation
        transcript = self.context_builder.build_context_for_api(
            channel_id=channel_id,
            pending_messages=[],
        )
        if not transcript:
            return

        # Append the proactive nudge as a user message
        full_prompt = f"{transcript}\n\n[系统提示] {proactive_prompt}"
        messages = [{"role": "user", "content": full_prompt}]

        try:
            response = await asyncio.to_thread(self.reply_service.generate_reply_with_tools, messages)
        except Exception:
            return

        reply = (response.text or "").strip()
        if not reply and not response.tool_calls:
            return
        if "[SILENT]" in reply:
            return

        # Send the proactive message and record it in history
        await send_callback(reply, response)
