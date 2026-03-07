from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime

import discord
from discord import AllowedMentions

from app.config.settings import BASE_DIR, Settings
from app.core.logging import BotLogger
from app.core.session_engine import SessionEngine
from app.infra.storage import ChatHistoryStorage
from app.services.reply_service import ReplyService


@dataclass
class TypingSession:
    started_at: float
    last_seen_at: float
    channel_label: str
    user_label: str


class DiscordBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = BotLogger(
            bot_key=settings.bot_key,
            mode=settings.app_mode,
            show_error_detail=settings.show_error_detail,
        )
        self.reply_service = ReplyService(settings)
        self.history_storage = ChatHistoryStorage(
            data_dir=BASE_DIR / "data" / "chat_history",
        )
        self.session_timeout_seconds = settings.session_timeout_seconds
        self.typing_timeout_seconds = settings.session_timeout_seconds
        self.typing_detect_delay_seconds = settings.typing_detect_delay_seconds
        self.reset_timer_seconds = settings.reset_timer_seconds
        self._typing_sessions: dict[tuple[int, int], TypingSession] = {}
        self._session_engine = SessionEngine()
        self._last_message_ts: dict[tuple[int, int], float] = {}
        self._typing_watchdog_task: asyncio.Task | None = None

        intents = discord.Intents.default()
        intents.message_content = True
        intents.typing = True
        self.client = discord.Client(intents=intents)

        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self.client.event(self.on_typing)

    def _typing_key(self, channel_id: int, user_id: int) -> tuple[int, int]:
        return (channel_id, user_id)

    @staticmethod
    def _now_clock() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _typing_probe_enabled(self) -> bool:
        return self.settings.app_mode == "debug" and self.settings.show_interaction_logs

    def _log_typing(self, message: str) -> None:
        if self._typing_probe_enabled():
            self.logger.info(message)

    @staticmethod
    def _channel_label(channel) -> str:  # type: ignore[no-untyped-def]
        name = getattr(channel, "name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
        return "DM"

    @staticmethod
    def _user_label(user) -> str:  # type: ignore[no-untyped-def]
        display_name = getattr(user, "display_name", None)
        if isinstance(display_name, str) and display_name.strip():
            return display_name.strip()
        name = getattr(user, "name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
        return str(getattr(user, "id", "unknown"))

    def _touch_typing_session(self, channel_id: int, user_id: int, *, channel_label: str, user_label: str) -> None:
        key = self._typing_key(channel_id, user_id)
        now = time.monotonic()
        session = self._typing_sessions.get(key)
        if session is None:
            self._typing_sessions[key] = TypingSession(
                started_at=now,
                last_seen_at=now,
                channel_label=channel_label,
                user_label=user_label,
            )
            since_last_msg = ""
            last_msg_ts = self._last_message_ts.get(key)
            if last_msg_ts is not None:
                since_last_msg = f" since_last_msg={now - last_msg_ts:.2f}s"
            self._log_typing(
                f"⌨️ typing_start user={user_label}{since_last_msg}"
            )
            return
        session.last_seen_at = now

    def _stop_typing_session(self, channel_id: int, user_id: int, reason: str) -> None:
        key = self._typing_key(channel_id, user_id)
        session = self._typing_sessions.pop(key, None)
        if session is None:
            return

        elapsed = time.monotonic() - session.started_at
        emoji = "🛑" if reason == "message" else "⏳"
        self._log_typing(
            f"{emoji} typing_stop user={session.user_label} "
            f"reason={reason} duration={elapsed:.2f}s"
        )

    def _touch_pending_activity(self, channel_id: int, user_id: int) -> None:
        self._session_engine.touch_activity(channel_id, user_id, now=time.monotonic())

    def _touch_pending_message(
        self,
        message: discord.Message,
        channel_id: int,
        user_id: int,
        user_label: str,
        text: str,
    ) -> None:
        now = time.monotonic()
        pending, opened = self._session_engine.touch_message(
            message=message,
            channel_id=channel_id,
            user_id=user_id,
            user_label=user_label,
            text=text,
            now=now,
            now_clock=self._now_clock(),
        )
        if opened:
            pending.task = asyncio.create_task(self._dispatch_after_idle(channel_id, user_id))
            self._log_typing(
                f"🧠🧠 buffer_open user={user_label}"
            )
            return
        self._log_typing(
            f"🧠 buffer_merge user={user_label} chunks={len(pending.chunks)}"
        )

    async def _dispatch_after_idle(self, channel_id: int, user_id: int) -> None:
        while True:
            pending = self._session_engine.get(channel_id, user_id)
            if pending is None:
                return

            now = time.monotonic()
            ready, sleep_seconds = self._session_engine.evaluate_wait(
                session=pending,
                now=now,
                typing_detect_delay_seconds=self.typing_detect_delay_seconds,
                reset_timer_seconds=self.reset_timer_seconds,
                session_timeout_seconds=self.session_timeout_seconds,
            )
            if ready:
                break

            await asyncio.sleep(sleep_seconds)

        pending = self._session_engine.pop(channel_id, user_id)
        if pending is None:
            return

        merged_text = "\n".join([chunk for chunk in pending.chunks if chunk]).strip()
        if not merged_text:
            return

        self.history_storage.append_entry(
            channel_id=channel_id,
            role="user",
            username=pending.user_label,
            time=pending.first_time,
            content=merged_text,
        )
        transcript = self.history_storage.build_transcript_for_api(
            channel_id=channel_id,
        )
        if not transcript:
            self.logger.error("LOGIC", "empty transcript, skip api request")
            return
        if self.settings.show_api_payload:
            self.logger.info(f"📨 api_payload\n{transcript}")
        messages = [{"role": "user", "content": transcript}]
        merged_one_line = merged_text.replace("\n", "\\n")
        now_send = time.monotonic()
        wait_from_last_msg = now_send - pending.last_message_at
        self.logger.info(
            f"✅ api_request_sent wait_from_last_msg={wait_from_last_msg:.2f}s includes={merged_one_line}"
        )

        try:
            async with pending.channel.typing():
                reply = await asyncio.to_thread(self.reply_service.generate_reply, messages)
            try:
                await pending.anchor_message.reply(
                    reply,
                    mention_author=False,
                    allowed_mentions=AllowedMentions.none(),
                )
            except Exception as exc:
                # Fallback only when reply reference is not supported for the current channel/message state.
                self.logger.error("LOGIC", f"reply() failed: {exc}")
                try:
                    await pending.anchor_message.reply(
                        reply,
                        mention_author=False,
                        allowed_mentions=AllowedMentions.none(),
                    )
                except Exception as exc2:
                    self.logger.error("LOGIC", f"reply() fallback failed: {exc2}")
                    await pending.channel.send(
                        reply,
                        allowed_mentions=AllowedMentions.none(),
                    )
            self.history_storage.append_entry(
                channel_id=channel_id,
                role="assistant",
                username=self.settings.bot_key,
                time=self._now_clock(),
                content=reply,
            )
            self._log_typing(
                f"🚀 api_sent user={pending.user_label} chunks={len(pending.chunks)} merged_len={len(merged_text)}"
            )
        except Exception as exc:  # noqa: BLE001
            # Keep this as UNKNOWN to preserve your current error filtering behavior.
            self.logger.error("UNKNOWN", "failed to send reply", exc=exc)
            try:
                await pending.anchor_message.reply(
                    "我刚刚有点卡住了，等我一下再试试。",
                    mention_author=False,
                    allowed_mentions=AllowedMentions.none(),
                )
            except Exception:
                await pending.channel.send("我刚刚有点卡住了，等我一下再试试。")

    async def _typing_watchdog(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            expired: list[tuple[int, int]] = []
            for key, session in self._typing_sessions.items():
                if now - session.last_seen_at >= self.typing_timeout_seconds:
                    expired.append(key)
            for channel_id, user_id in expired:
                self._stop_typing_session(channel_id, user_id, reason="timeout")

    async def on_ready(self) -> None:
        self.logger.startup_jar(cat_count=1)
        self.logger.info(f"bot is running as {self.client.user}")
        if self._typing_probe_enabled() and (self._typing_watchdog_task is None or self._typing_watchdog_task.done()):
            self._typing_watchdog_task = asyncio.create_task(self._typing_watchdog())

    async def on_typing(self, channel, user, when):  # type: ignore[no-untyped-def]
        if user.bot:
            return
        if not hasattr(channel, "id"):
            return
        self._touch_typing_session(
            channel.id,
            user.id,
            channel_label=self._channel_label(channel),
            user_label=self._user_label(user),
        )
        self._touch_pending_activity(channel.id, user.id)
        self._session_engine.switch_to_long_timer(channel.id, user.id)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        text = (message.content or "").strip()
        if not text:
            return
        key = self._typing_key(message.channel.id, message.author.id)
        self._last_message_ts[key] = time.monotonic()

        self._stop_typing_session(message.channel.id, message.author.id, reason="message")
        self._touch_pending_message(
            message=message,
            channel_id=message.channel.id,
            user_id=message.author.id,
            user_label=self._user_label(message.author),
            text=text,
        )

    def run_forever(self) -> None:
        if not self.settings.discord_bot_token:
            self.logger.error("CONFIG", "missing DISCORD_BOT_TOKEN")
            self.logger.startup_jar(cat_count=0)
            return

        try:
            self.client.run(self.settings.discord_bot_token)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("API", "discord client failed to start", exc=exc)
            self.logger.startup_jar(cat_count=0)
