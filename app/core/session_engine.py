from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class PendingReplySession:
    channel: Any
    anchor_message: Any
    user_label: str
    first_at: float
    first_time: str
    last_activity_at: float
    last_message_at: float
    use_long_timer: bool
    chunks: list[str]
    task: asyncio.Task | None = None


class SessionEngine:
    def __init__(self) -> None:
        self._sessions: dict[tuple[int, int], PendingReplySession] = {}

    @staticmethod
    def key(channel_id: int, user_id: int) -> tuple[int, int]:
        return (channel_id, user_id)

    def get(self, channel_id: int, user_id: int) -> PendingReplySession | None:
        return self._sessions.get(self.key(channel_id, user_id))

    def pop(self, channel_id: int, user_id: int) -> PendingReplySession | None:
        return self._sessions.pop(self.key(channel_id, user_id), None)

    def touch_activity(self, channel_id: int, user_id: int, *, now: float) -> None:
        session = self.get(channel_id, user_id)
        if session is None:
            return
        session.last_activity_at = now

    def switch_to_long_timer(self, channel_id: int, user_id: int) -> None:
        session = self.get(channel_id, user_id)
        if session is None:
            return
        session.use_long_timer = True

    def touch_message(
        self,
        *,
        message: Any,
        channel_id: int,
        user_id: int,
        user_label: str,
        text: str,
        now: float,
        now_clock: str,
    ) -> tuple[PendingReplySession, bool]:
        key = self.key(channel_id, user_id)
        session = self._sessions.get(key)
        if session is None:
            session = PendingReplySession(
                channel=message.channel,
                anchor_message=message,
                user_label=user_label,
                first_at=now,
                first_time=now_clock,
                last_activity_at=now,
                last_message_at=now,
                use_long_timer=False,
                chunks=[text],
            )
            self._sessions[key] = session
            return session, True

        session.use_long_timer = False
        session.anchor_message = message
        session.chunks.append(text)
        session.last_activity_at = now
        session.last_message_at = now
        return session, False

    @staticmethod
    def evaluate_wait(
        *,
        session: PendingReplySession,
        now: float,
        typing_detect_delay_seconds: float,
        reset_timer_seconds: float,
        session_timeout_seconds: float,
    ) -> tuple[bool, float]:
        idle_for = now - session.last_activity_at
        total_for = now - session.first_at

        if total_for < typing_detect_delay_seconds:
            return False, max(0.05, min(0.5, typing_detect_delay_seconds - total_for))

        if session.use_long_timer:
            if idle_for >= session_timeout_seconds:
                return True, 0.0
            wait_idle = session_timeout_seconds - idle_for
        else:
            if idle_for >= reset_timer_seconds:
                return True, 0.0
            wait_idle = reset_timer_seconds - idle_for

        if total_for >= max(session_timeout_seconds * 4, 60.0):
            return True, 0.0

        return False, max(0.05, min(0.5, wait_idle))
