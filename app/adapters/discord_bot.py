from __future__ import annotations

import asyncio
import queue as _queue
import re
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time

from app.core.clock import now as _now, now_clock as _now_clock_util

import discord
from discord import AllowedMentions, app_commands

from app.config.settings import BASE_DIR, Settings, env_last_modified, load_settings
from app.core.logging import BotLogger
from app.core.session_engine import SessionEngine
from app.infra.storage import ChatHistoryStore, CompressionStore
from app.services.compression_service import CompressionService
from app.services.context_builder import ContextBuilder
from app.services.prompt_service import PromptService
from app.services.reply_service import ReplyService
from app.adapters.discord_ui import ToolboxView


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
        self.history_store = ChatHistoryStore(
            data_dir=BASE_DIR / "data" / "chat_history",
        )
        self.compression_store = CompressionStore(
            memory_dir=BASE_DIR / "data" / "memory",
        )
        self.compression_service = CompressionService(
            settings=settings,
            history_store=self.history_store,
            compression_store=self.compression_store,
        )
        self.prompt_service = PromptService()
        self.context_builder = ContextBuilder(self.history_store, self.compression_store)
        self.proactive_idle_seconds = settings.proactive_idle_seconds
        self.typing_nudge_seconds = settings.typing_nudge_seconds
        self.watch_online_idle_seconds = settings.watch_online_idle_seconds
        self.session_timeout_seconds = settings.session_timeout_seconds
        self.typing_detect_delay_seconds = settings.typing_detect_delay_seconds
        self.reset_timer_seconds = settings.reset_timer_seconds
        self._env_watch_task: asyncio.Task | None = None
        self._env_mtime = env_last_modified()
        self._typing_sessions: dict[tuple[int, int], TypingSession] = {}
        self._session_engine = SessionEngine()
        self._last_message_ts: dict[tuple[int, int], float] = {}
        self._typing_watchdog_task: asyncio.Task | None = None
        self._variable_timers: dict[int, tuple[asyncio.Task, float]] = {}  # (task, deadline)
        self._alarms: dict[int, list[asyncio.Task]] = {}  # per-channel, multiple allowed
        self._pending_alarm_reasons: dict[int, list[str]] = {}  # buffered for next timer fire
        self._pending_reactions: dict[int, list[str]] = {}  # buffered reactions for next timer fire
        self._typing_nudge_channels: set[int] = set()  # channels with a pending typing-nudge timer
        self._quiet_buffered_reasons: dict[int, list[str]] = {}  # buffered during quiet hours
        self._quiet_channels: dict[int, discord.abc.Messageable] = {}  # channel refs for flush
        self._quiet_flush_task: asyncio.Task | None = None

        intents = discord.Intents.default()
        intents.message_content = True
        intents.typing = True
        intents.reactions = True
        intents.presences = True
        intents.members = True
        self.client = discord.Client(intents=intents)
        self.tree = app_commands.CommandTree(self.client)
        self._commands_synced = False

        self._watch_previous_status: dict[int, str] = {}  # user_id -> previous status
        self._watch_online_timers: dict[int, asyncio.Task] = {}  # user_id -> pending timer
        self._jealousy_counts: dict[int, int] = {}  # user_id -> accumulated typing count
        self._jealousy_timers: dict[int, asyncio.Task] = {}  # user_id -> pending fire task

        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self.client.event(self.on_message_edit)
        self.client.event(self.on_typing)
        self.client.event(self.on_raw_reaction_add)
        self.client.event(self.on_guild_channel_delete)
        self.client.event(self.on_presence_update)
        self._register_app_commands()

    def _register_app_commands(self) -> None:
        @self.tree.command(name="工具箱", description="打开工具箱")
        async def toolbox(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                "工具箱",
                view=ToolboxView(self),
                ephemeral=True,
            )

        @self.tree.command(name="compress", description="Compress active chat history for this channel")
        async def compress(interaction: discord.Interaction) -> None:
            channel = interaction.channel
            if channel is None or not hasattr(channel, "id"):
                await interaction.response.send_message(
                    "当前上下文没有可用频道，不能执行压缩。",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)

            try:
                segment = await asyncio.to_thread(
                    self.compression_service.compress_history,
                    channel_id=channel.id,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "manual compression failed", exc=exc)
                await interaction.followup.send(
                    "压缩失败了，去日志里看一下。",
                    ephemeral=True,
                )
                return

            if segment is None:
                await interaction.followup.send(
                    "这个频道当前没有可压缩的活跃消息。",
                    ephemeral=True,
                )
                return

            keywords = segment.get("keywords") or []
            keywords_text = "、".join(str(item) for item in keywords) if keywords else "无"
            await interaction.followup.send(
                "\n".join(
                    [
                        "压缩完成。",
                        f"segment_id: {segment.get('segment_id', '')}",
                        f"source_id: {segment.get('source_id', '')}",
                        f"范围: {segment.get('start_time', '')} -> {segment.get('end_time', '')}",
                        f"条数: {segment.get('message_count', 0)}",
                        f"关键词: {keywords_text}",
                    ]
                ),
                ephemeral=True,
            )

    def apply_settings(self, settings: Settings) -> None:
        old_token = self.settings.discord_bot_token
        self.settings = settings
        self.reply_service.apply_settings(settings)
        self.compression_service.apply_settings(settings)
        self.logger.bot_key = settings.bot_key
        self.logger.mode = settings.app_mode
        self.logger.show_error_detail = settings.show_error_detail
        self.session_timeout_seconds = settings.session_timeout_seconds
        self.typing_detect_delay_seconds = settings.typing_detect_delay_seconds
        self.reset_timer_seconds = settings.reset_timer_seconds
        self.proactive_idle_seconds = settings.proactive_idle_seconds
        self.typing_nudge_seconds = settings.typing_nudge_seconds
        self.watch_online_idle_seconds = settings.watch_online_idle_seconds
        if settings.discord_bot_token != old_token:
            self.logger.error(
                "CONFIG",
                "DISCORD_BOT_TOKEN changed in .env but live token swap is not supported; restart required.",
            )

    async def reload_settings_if_needed(self) -> bool:
        current_mtime = env_last_modified()
        if current_mtime <= self._env_mtime:
            return False

        settings = load_settings()
        self.apply_settings(settings)
        self._env_mtime = current_mtime
        self.logger.info("env hot-reloaded from .env")
        return True

    async def _watch_env_changes(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.reload_settings_if_needed()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("CONFIG", "failed to hot reload .env", exc=exc)

    def _typing_key(self, channel_id: int, user_id: int) -> tuple[int, int]:
        return (channel_id, user_id)

    @staticmethod
    def _now_clock() -> str:
        return _now_clock_util()

    def _typing_probe_enabled(self) -> bool:
        return self.settings.app_mode == "debug" and self.settings.show_interaction_logs

    def _log_typing(self, message: str) -> None:
        if self._typing_probe_enabled():
            self.logger.info(message)

    @staticmethod
    def _split_sentences(text: str, *, split: bool = True) -> list[str]:
        stripped = (text or "").strip()
        if not stripped:
            return []
        if not split:
            return [stripped]
        parts = re.split(r"\n+", stripped)
        out = [p.strip() for p in parts if p and p.strip()]
        return out or [stripped]

    async def _reply_by_sentence(
        self,
        anchor_message: discord.Message | None,
        reply: str,
        *,
        channel: discord.abc.Messageable | None = None,
    ) -> None:
        do_split = self.settings.split_mode == "chat"
        sentences = self._split_sentences(reply, split=do_split)
        if not sentences:
            return
        target_channel = channel or (anchor_message.channel if anchor_message else None)
        if target_channel is None:
            return
        for idx, sentence in enumerate(sentences):
            if idx == 0 and anchor_message is not None:
                await anchor_message.reply(
                    sentence,
                    mention_author=False,
                    allowed_mentions=AllowedMentions.none(),
                )
            else:
                await target_channel.send(
                    sentence,
                    allowed_mentions=AllowedMentions.none(),
                )
            if idx < len(sentences) - 1:
                await asyncio.sleep(0.8)

    async def _stream_and_send(
        self,
        anchor_message: discord.Message | None,
        channel: discord.abc.Messageable,
        messages: list[dict[str, str]],
        *,
        include_tools: bool = False,
    ) -> LLMResponse:
        """Stream LLM response, sending each sentence to Discord as it completes."""
        from app.infra.llm_client import LLMResponse

        q: _queue.Queue[tuple[str, object]] = _queue.Queue()

        def _produce() -> None:
            try:
                resp = self.reply_service.stream_reply_with_tools(
                    messages, lambda chunk: q.put(("text", chunk)),
                    include_tools=include_tools,
                )
                q.put(("done", resp))
            except Exception as exc:  # noqa: BLE001
                q.put(("error", exc))

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _produce)

        buffer = ""
        is_first = True

        while True:
            try:
                kind, value = q.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.05)
                continue

            if kind == "text":
                buffer += value
                if self.settings.split_mode == "chat":
                    parts = self._split_sentences(buffer)
                    if len(parts) > 1:
                        delay = self.settings.chat_reply_delay_seconds
                        for s in parts[:-1]:
                            if not is_first and delay > 0:
                                async with channel.typing():
                                    await asyncio.sleep(delay)
                            if is_first and anchor_message is not None:
                                await anchor_message.reply(
                                    s, mention_author=False,
                                    allowed_mentions=AllowedMentions.none(),
                                )
                            else:
                                await channel.send(
                                    s, allowed_mentions=AllowedMentions.none(),
                                )
                            is_first = False
                        buffer = parts[-1]
            elif kind == "done":
                if buffer.strip():
                    if not is_first and self.settings.chat_reply_delay_seconds > 0:
                        async with channel.typing():
                            await asyncio.sleep(self.settings.chat_reply_delay_seconds)
                    if is_first and anchor_message is not None:
                        await anchor_message.reply(
                            buffer.strip(), mention_author=False,
                            allowed_mentions=AllowedMentions.none(),
                        )
                    else:
                        await channel.send(
                            buffer.strip(), allowed_mentions=AllowedMentions.none(),
                        )
                return value  # type: ignore[return-value]
            elif kind == "error":
                raise value  # type: ignore[misc]

        return LLMResponse(text="")  # unreachable

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

    def _touch_typing_session(self, channel_id: int, user_id: int, *, channel_label: str, user_label: str) -> bool:
        """Track typing. Returns True if this is a *new* typing session."""
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
            return True
        session.last_seen_at = now
        return False

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

        pending_entry = {
            "role": "user",
            "username": pending.user_label,
            "time": pending.first_time,
            "content": merged_text,
        }
        transcript = self.context_builder.build_context_for_api(
            channel_id=channel_id,
            pending_messages=[pending_entry],
        )
        if not transcript:
            self.logger.error("LOGIC", "empty transcript, skip api request")
            return
        self.history_store.append_entry(
            channel_id=channel_id,
            role="user",
            username=pending.user_label,
            time=pending.first_time,
            content=merged_text,
        )
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
            await pending.channel.typing()
            response = await self._stream_and_send(
                pending.anchor_message, pending.channel, messages,
            )
            reply = (response.text or "").strip()
            if reply:
                self.history_store.append_entry(
                    channel_id=channel_id,
                    role="assistant",
                    username=self.settings.bot_key,
                    time=self._now_clock(),
                    content=reply,
                )
            self._log_typing(
                f"🚀 api_sent user={pending.user_label} chunks={len(pending.chunks)} merged_len={len(merged_text)}"
            )
            # Handle tool calls from LLM
            await self._handle_tool_calls(response, channel_id, pending.channel, prior_messages=messages)
            # Start proactive idle timer after bot replies
            self._schedule_proactive(channel_id, pending.channel)
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

    async def _reply_immediate(self, message: discord.Message, text: str) -> None:
        """Skip typing-wait; build context and call API right away."""
        channel_id = message.channel.id
        user_label = self._user_label(message.author)
        now_clock = self._now_clock()

        pending_entry = {
            "role": "user",
            "username": user_label,
            "time": now_clock,
            "content": text,
        }
        transcript = self.context_builder.build_context_for_api(
            channel_id=channel_id,
            pending_messages=[pending_entry],
        )
        if not transcript:
            self.logger.error("LOGIC", "empty transcript, skip api request")
            return
        self.history_store.append_entry(
            channel_id=channel_id,
            role="user",
            username=user_label,
            time=now_clock,
            content=text,
        )
        if self.settings.show_api_payload:
            self.logger.info(f"📨 api_payload\n{transcript}")
        messages = [{"role": "user", "content": transcript}]
        text_one_line = text.replace("\n", "\\n")
        self.logger.info(f"✅ api_request_sent (immediate) includes={text_one_line}")

        try:
            await message.channel.typing()
            response = await self._stream_and_send(
                message, message.channel, messages,
            )
            reply = (response.text or "").strip()
            if reply:
                self.history_store.append_entry(
                    channel_id=channel_id,
                    role="assistant",
                    username=self.settings.bot_key,
                    time=self._now_clock(),
                    content=reply,
                )
            await self._handle_tool_calls(response, channel_id, message.channel, prior_messages=messages)
            self._schedule_proactive(channel_id, message.channel)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "failed to send reply", exc=exc)
            try:
                await message.reply(
                    "我刚刚有点卡住了，等我一下再试试。",
                    mention_author=False,
                    allowed_mentions=AllowedMentions.none(),
                )
            except Exception:
                await message.channel.send("我刚刚有点卡住了，等我一下再试试。")

    async def _handle_tool_calls(
        self,
        response,
        channel_id: int,
        channel: discord.abc.Messageable,
        *,
        prior_messages: list[dict[str, str]] | None = None,
    ) -> None:
        """Process tool calls returned by the LLM."""
        for tc in response.tool_calls:
            if tc.name == "set_timer":
                seconds = tc.input.get("seconds", 0)
                reason = tc.input.get("reason") or None
                if isinstance(seconds, (int, float)) and seconds > 0:
                    if reason:
                        self._schedule_alarm(channel_id, channel, seconds, reason)
                    else:
                        self._schedule_variable_timer(channel_id, channel, seconds, source="llm")
            elif tc.name == "web_search":
                query = tc.input.get("query", "")
                if query:
                    await self._execute_search(query, channel_id, channel, prior_messages)

    async def _execute_search(
        self,
        query: str,
        channel_id: int,
        channel: discord.abc.Messageable,
        prior_messages: list[dict[str, str]] | None = None,
    ) -> None:
        """Run a web search and feed results back to the LLM for a final reply."""
        from app.infra.search_client import web_search

        self.logger.info(f"🔍 web_search query={query}")
        status_msg = None
        try:
            status_msg = await channel.send(f"正在搜索...")
        except Exception:  # noqa: BLE001
            pass
        try:
            results = await asyncio.to_thread(web_search, query)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "web search failed", exc=exc)
            results = []
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:  # noqa: BLE001
                pass

        if results:
            lines = [f"[搜索结果: {query}]"]
            for r in results:
                lines.append(f"- {r['title']}\n  {r['body']}\n  {r['href']}")
            search_block = "\n".join(lines)
        else:
            search_block = f"[搜索结果: {query}]\n未找到相关结果。"

        messages = list(prior_messages) if prior_messages else []
        messages.append({"role": "user", "content": search_block})

        try:
            async with channel.typing():
                search_response = await asyncio.to_thread(
                    self.reply_service.generate_reply_with_tools, messages, include_tools=True,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "search follow-up api request failed", exc=exc)
            return

        reply = (search_response.text or "").strip()
        if reply:
            try:
                await self._reply_by_sentence(None, reply, channel=channel)
                self.history_store.append_entry(
                    channel_id=channel_id,
                    role="assistant",
                    username=self.settings.bot_key,
                    time=self._now_clock(),
                    content=reply,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "failed to send search reply", exc=exc)

        await self._handle_tool_calls(search_response, channel_id, channel, prior_messages=messages)

    def _schedule_variable_timer(
        self,
        channel_id: int,
        channel: discord.abc.Messageable,
        seconds: float,
        *,
        source: str = "auto",
    ) -> None:
        """Schedule a variable timer."""
        # Cancel any existing variable timer for this channel
        old = self._variable_timers.pop(channel_id, None)
        if old is not None:
            task, _ = old
            if not task.done():
                task.cancel()
        deadline = time.monotonic() + seconds
        new_task = asyncio.create_task(
            self._variable_timer_fire(channel_id, channel, seconds)
        )
        self._variable_timers[channel_id] = (new_task, deadline)
        icon = "🤖" if source == "llm" else "⏳"
        self._log_typing(f"{icon} timer_start ch={channel_id} s={seconds}")

    def _schedule_alarm(
        self,
        channel_id: int,
        channel: discord.abc.Messageable,
        seconds: float,
        reason: str,
    ) -> None:
        """Schedule an alarm. Multiple alarms per channel; not cancelled by user messages."""
        task = asyncio.create_task(
            self._alarm_fire(channel_id, channel, seconds, reason)
        )
        self._alarms.setdefault(channel_id, []).append(task)

    async def _variable_timer_fire(
        self,
        channel_id: int,
        channel: discord.abc.Messageable,
        seconds: float,
    ) -> None:
        """Wait for the specified duration, then send context to AI."""
        await asyncio.sleep(seconds)
        self._variable_timers.pop(channel_id, None)

        # During quiet hours, discard proactive/variable timer fires silently
        if self._is_quiet_time():
            self._quiet_channels.setdefault(channel_id, channel)
            self._schedule_quiet_flush()
            self._log_typing(f"🤫 timer_quiet ch={channel_id} s={seconds}")
            return

        recent = self.history_store.load_all_entries(channel_id=channel_id)[-20:]
        transcript = self.history_store.render_entries(recent) if recent else ""
        is_typing_nudge = channel_id in self._typing_nudge_channels
        self._typing_nudge_channels.discard(channel_id)
        if is_typing_nudge:
            timer_note = "[系统提示] ta刚才在打字，但最终没有发出消息。"
        elif seconds != self.proactive_idle_seconds:
            timer_note = f"[system: your set_timer for {seconds}s has expired]\n{self.prompt_service.read_prompt('proactive')}"
        else:
            timer_note = f"[系统提示] {self.prompt_service.read_prompt('proactive')}"
        # Attach any buffered alarm reasons
        pending_reasons = self._pending_alarm_reasons.pop(channel_id, [])
        if pending_reasons:
            alarm_lines = "\n".join(f"- {r}" for r in pending_reasons)
            timer_note += (
                f"\n[system: 以下闹钟已到期，你必须提醒用户这些事情，不可以沉默]\n{alarm_lines}"
            )
        # Attach any buffered reactions
        pending_reactions = self._pending_reactions.pop(channel_id, [])
        if pending_reactions:
            reaction_lines = "\n".join(f"- {r}" for r in pending_reactions)
            timer_note += f"\n[system: 用户在你空闲期间添加了以下表情反应]\n{reaction_lines}"
        if transcript:
            transcript = f"{transcript}\n{timer_note}"
        else:
            transcript = timer_note

        messages = [{"role": "user", "content": transcript}]
        try:
            async with channel.typing():
                response = await asyncio.to_thread(
                    self.reply_service.generate_reply_with_tools, messages, include_tools=True,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "variable timer api request failed", exc=exc)
            return

        reply = (response.text or "").strip()
        if reply and "[SILENT]" not in reply:
            try:
                await self._reply_by_sentence(None, reply, channel=channel)
                self.history_store.append_entry(
                    channel_id=channel_id,
                    role="assistant",
                    username=self.settings.bot_key,
                    time=self._now_clock(),
                    content=reply,
                )
                self._log_typing(f"⏰ timer_sent ch={channel_id} reply={reply}")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "failed to send variable timer message", exc=exc)
        else:
            self._log_typing(f"🔇 time_fire ch={channel_id}")

        # Handle any new tool calls (e.g. AI sets another timer)
        await self._handle_tool_calls(response, channel_id, channel, prior_messages=messages)

    async def _alarm_fire(
        self,
        channel_id: int,
        channel: discord.abc.Messageable,
        seconds: float,
        reason: str,
    ) -> None:
        """Wait for the specified duration, then remind the user. Cannot be silent."""
        task = asyncio.current_task()
        try:
            await asyncio.sleep(seconds)
        finally:
            # Remove ourselves from the alarm list
            alarm_list = self._alarms.get(channel_id, [])
            if task in alarm_list:
                alarm_list.remove(task)
            if not alarm_list:
                self._alarms.pop(channel_id, None)

        # During quiet hours, buffer alarm for flush at quiet end
        if self._is_quiet_time():
            self._quiet_buffered_reasons.setdefault(channel_id, []).append(reason)
            self._quiet_channels[channel_id] = channel
            self._schedule_quiet_flush()
            self.logger.info(f"🤫 alarm_buffered_quiet channel={channel_id} reason={reason}")
            return

        # If a variable/proactive timer is about to fire soon, buffer this alarm
        vt = self._variable_timers.get(channel_id)
        if vt is not None:
            _, deadline = vt
            remaining = deadline - time.monotonic()
            if remaining < self.proactive_idle_seconds:
                self._pending_alarm_reasons.setdefault(channel_id, []).append(reason)
                self.logger.info(f"⏰ alarm_buffered channel={channel_id} reason={reason} remaining={remaining:.0f}s")
                return

        recent = self.history_store.load_all_entries(channel_id=channel_id)[-10:]
        history_block = self.history_store.render_entries(recent) if recent else ""
        alarm_note = (
            f"[system: your set_timer for {seconds}s has expired]\n"
            f"你之前答应提醒用户：{reason}\n"
            "请现在提醒用户这件事，不可以沉默。保持你一贯的说话风格和人格。"
        )
        transcript = f"{history_block}\n{alarm_note}".strip()

        messages = [{"role": "user", "content": transcript}]
        try:
            async with channel.typing():
                response = await asyncio.to_thread(
                    self.reply_service.generate_reply_with_tools, messages, include_tools=True,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "alarm api request failed", exc=exc)
            return

        reply = (response.text or "").strip()
        if reply:
            try:
                await self._reply_by_sentence(None, reply, channel=channel)
                self.history_store.append_entry(
                    channel_id=channel_id,
                    role="assistant",
                    username=self.settings.bot_key,
                    time=self._now_clock(),
                    content=reply,
                )
                self._log_typing(f"⏰ alarm_sent ch={channel_id} reason={reason} reply={reply}")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "failed to send alarm message", exc=exc)

        await self._handle_tool_calls(response, channel_id, channel, prior_messages=messages)

    def _schedule_proactive(self, channel_id: int, channel: discord.abc.Messageable) -> None:
        """Schedule proactive idle timer — uses the same slot as variable timer."""
        self._typing_nudge_channels.discard(channel_id)
        if self.proactive_idle_seconds <= 0:
            return
        self._schedule_variable_timer(channel_id, channel, self.proactive_idle_seconds)

    def _maybe_schedule_typing_nudge(
        self, channel_id: int, channel: discord.abc.Messageable,
    ) -> None:
        """Schedule a typing-nudge, but only if it's shorter than the current timer."""
        nudge = self.typing_nudge_seconds
        vt = self._variable_timers.get(channel_id)
        if vt is not None:
            _, deadline = vt
            remaining = deadline - time.monotonic()
            if remaining <= nudge:
                return  # existing timer fires sooner, keep it
        self._typing_nudge_channels.add(channel_id)
        self._schedule_variable_timer(channel_id, channel, nudge, source="typing_nudge")

    @staticmethod
    def _parse_time(s: str) -> dt_time | None:
        """Parse 'HH:MM' into a time object, or None on failure."""
        s = s.strip()
        if not s:
            return None
        try:
            parts = s.split(":")
            return dt_time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return None

    def _is_quiet_time(self) -> bool:
        """Return True if quiet hours are active right now."""
        if not self.settings.quiet_enabled:
            return False
        start = self._parse_time(self.settings.quiet_start)
        end = self._parse_time(self.settings.quiet_end)
        if start is None or end is None:
            return False
        now = _now().time()
        if start <= end:
            return start <= now < end
        # Crosses midnight, e.g. 23:00 -> 07:00
        return now >= start or now < end

    def _seconds_until_quiet_end(self) -> float:
        """Return seconds until quiet_end. Assumes we are currently in quiet time."""
        end = self._parse_time(self.settings.quiet_end)
        if end is None:
            return 0.0
        now = _now()
        end_today = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        if end_today <= now:
            # End is tomorrow
            from datetime import timedelta
            end_today += timedelta(days=1)
        return (end_today - now).total_seconds()

    def _schedule_quiet_flush(self) -> None:
        """Ensure a flush task is scheduled for when quiet hours end."""
        if self._quiet_flush_task is not None and not self._quiet_flush_task.done():
            return  # already scheduled
        wait = self._seconds_until_quiet_end()
        if wait <= 0:
            return
        self._quiet_flush_task = asyncio.create_task(self._quiet_flush_fire(wait))

    async def _quiet_flush_fire(self, wait_seconds: float) -> None:
        """Sleep until quiet hours end, then send a morning message per channel."""
        await asyncio.sleep(wait_seconds)
        # Drain state
        buffered = dict(self._quiet_buffered_reasons)
        channels = dict(self._quiet_channels)
        self._quiet_buffered_reasons.clear()
        self._quiet_channels.clear()

        morning_prompt = self.prompt_service.read_prompt("morning")
        proactive_prompt = self.prompt_service.read_prompt("proactive")

        for channel_id, channel in channels.items():
            recent = self.history_store.load_all_entries(channel_id=channel_id)[-10:]
            history_block = self.history_store.render_entries(recent) if recent else ""

            parts: list[str] = []
            if history_block:
                parts.append(history_block)
            parts.append(f"[system: 静默时间已结束]\n{morning_prompt}")
            # Attach buffered alarms if any
            reasons = buffered.get(channel_id, [])
            if reasons:
                alarm_lines = "\n".join(f"- {r}" for r in reasons)
                parts.append(
                    "[system: 以下闹钟在静默期间到期，你必须提醒用户这些事情，不可以沉默]\n"
                    f"{alarm_lines}"
                )
            parts.append(f"[system] {proactive_prompt}")
            transcript = "\n".join(parts)

            messages = [{"role": "user", "content": transcript}]
            try:
                async with channel.typing():
                    response = await asyncio.to_thread(
                    self.reply_service.generate_reply_with_tools, messages, include_tools=True,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "quiet flush api request failed", exc=exc)
                continue

            reply = (response.text or "").strip()
            if reply:
                try:
                    await self._reply_by_sentence(None, reply, channel=channel)
                    self.history_store.append_entry(
                        channel_id=channel_id,
                        role="assistant",
                        username=self.settings.bot_key,
                        time=self._now_clock(),
                        content=reply,
                    )
                    self._log_typing(f"🌅 morning ch={channel_id} alarms={len(reasons)}")
                except Exception as exc:  # noqa: BLE001
                    self.logger.error("UNKNOWN", "failed to send morning message", exc=exc)

            await self._handle_tool_calls(response, channel_id, channel, prior_messages=messages)

    async def _typing_watchdog(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            expired: list[tuple[int, int]] = []
            for key, session in self._typing_sessions.items():
                if now - session.last_seen_at >= self.session_timeout_seconds:
                    expired.append(key)
            for channel_id, user_id in expired:
                self._stop_typing_session(channel_id, user_id, reason="timeout")

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """When a channel is deleted, clean up orphaned data files."""
        self.logger.info(f"🗑️ channel_deleted id={channel.id} name={getattr(channel, 'name', '?')}")
        await self._cleanup_orphaned_data()

    async def _cleanup_orphaned_data(self) -> None:
        """Remove history/memory for channels that no longer exist in any guild."""
        all_guild_channel_ids: set[int] = set()
        for guild in self.client.guilds:
            for ch in guild.channels:
                all_guild_channel_ids.add(ch.id)
            for thread in guild.threads:
                all_guild_channel_ids.add(thread.id)

        stored_ids = self.history_store.all_channel_ids() | self.compression_store.all_channel_ids()
        orphans = stored_ids - all_guild_channel_ids
        if not orphans:
            return

        for cid in orphans:
            h = self.history_store.delete_channel(cid)
            m = self.compression_store.delete_channel(cid)
            if h or m:
                self.logger.info(f"🧹 cleaned_orphan channel_id={cid} history={h} memory={m}")

    async def _collect_bot_reply_batch(
        self,
        channel: discord.abc.Messageable,
        anchor: discord.Message,
    ) -> list[discord.Message] | None:
        """Find the full batch of consecutive bot messages around the anchor.

        Returns the batch if it is the last reply in the channel (no user
        messages after it), otherwise returns None.
        """
        bot_id = self.client.user.id  # type: ignore[union-attr]
        batch = [anchor]
        # Scan backward: collect consecutive bot messages before the anchor
        async for msg in channel.history(before=anchor, limit=50):  # type: ignore[union-attr]
            if msg.author.id == bot_id:
                batch.append(msg)
            else:
                break
        # Scan forward: collect consecutive bot messages after the anchor
        async for msg in channel.history(after=anchor, limit=50):  # type: ignore[union-attr]
            if msg.author.id == bot_id:
                batch.append(msg)
            else:
                return None  # not the last reply — user message found after batch
        return batch

    async def _delete_messages(self, messages: list[discord.Message]) -> None:
        if not messages:
            return
        channel = messages[0].channel
        # bulk delete is much faster (one API call for up to 100 messages)
        if hasattr(channel, "delete_messages") and len(messages) > 1:
            try:
                await channel.delete_messages(messages)  # type: ignore[union-attr]
                return
            except Exception:  # noqa: BLE001
                pass  # fallback to one-by-one
        for msg in messages:
            try:
                await msg.delete()
            except Exception:  # noqa: BLE001
                pass

    def _delete_bot_reply_db(self, channel_id: int) -> None:
        """Delete the last assistant entry from DB."""
        self.history_store.pop_last_by_role(channel_id=channel_id, role="assistant")

    async def _regenerate_reply(
        self,
        channel_id: int,
        channel: discord.abc.Messageable,
    ) -> None:
        """Re-generate a bot reply from the current history state."""
        transcript = self.context_builder.build_context_for_api(
            channel_id=channel_id,
            pending_messages=[],
        )
        if not transcript:
            return
        messages = [{"role": "user", "content": transcript}]
        try:
            async with channel.typing():
                response = await self._stream_and_send(
                    None, channel, messages,
                )
            reply = (response.text or "").strip()
            if reply:
                self.history_store.append_entry(
                    channel_id=channel_id,
                    role="assistant",
                    username=self.settings.bot_key,
                    time=self._now_clock(),
                    content=reply,
                )
            await self._handle_tool_calls(response, channel_id, channel, prior_messages=messages)
            self._schedule_proactive(channel_id, channel)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "regenerate reply failed", exc=exc)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.author.bot:
            return
        new_text = (after.content or "").strip()
        if not new_text:
            return
        channel_id = after.channel.id
        entries = self.history_store.load_all_entries(channel_id=channel_id)
        if not entries:
            return
        # Check the last entry is from the bot (i.e. there's a reply to regenerate)
        if entries[-1].get("role") != "assistant":
            return
        # Find the last user entry and verify it matches the edited message
        old_text = (before.content or "").strip()
        for e in reversed(entries):
            if e["role"] == "user":
                # If before content is available, verify it matches
                if old_text and e["content"] != old_text:
                    return
                break
        else:
            return  # no user entry found

        # Delete bot's Discord messages after the edited user message
        to_delete: list[discord.Message] = []
        bot_id = self.client.user.id  # type: ignore[union-attr]
        async for msg in after.channel.history(after=after, limit=50):
            if msg.author.id == bot_id:
                to_delete.append(msg)
            else:
                break
        await self._delete_messages(to_delete)

        # Update user's last message, delete bot reply from DB, regenerate
        self.history_store.replace_last_by_role(
            channel_id=channel_id, role="user", new_content=new_text,
        )
        self._delete_bot_reply_db(channel_id)
        await self._regenerate_reply(channel_id, after.channel)

    async def on_ready(self) -> None:
        self.logger.startup_jar(cat_count=1)
        self.logger.info(f"bot is running as {self.client.user}")
        if not self._commands_synced:
            try:
                synced = await self.tree.sync()
                self._commands_synced = True
                self.logger.info(f"slash commands synced: {len(synced)}")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("API", "failed to sync slash commands", exc=exc)
        if self._typing_probe_enabled() and (self._typing_watchdog_task is None or self._typing_watchdog_task.done()):
            self._typing_watchdog_task = asyncio.create_task(self._typing_watchdog())
        if self._env_watch_task is None or self._env_watch_task.done():
            self._env_watch_task = asyncio.create_task(self._watch_env_changes())
        await self._cleanup_orphaned_data()

    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        uid_str = str(after.id)
        if uid_str not in self.settings.watch_user_ids:
            return
        old_status = str(before.status)
        new_status = str(after.status)
        if old_status == new_status:
            return
        prev = self._watch_previous_status.get(after.id)
        self._watch_previous_status[after.id] = new_status
        name = after.display_name
        self.logger.info(f"👁️ presence {name}({uid_str}): {old_status} -> {new_status}")
        # Skip initial cache fill from Discord
        if prev is None:
            return
        # offline/invisible -> online/idle/dnd: start 10-min idle check
        if old_status == "offline" and new_status != "offline":
            self._start_watch_timer(after.id, after.guild)
        # online -> offline: cancel any pending timer
        elif new_status == "offline":
            self._cancel_watch_timer(after.id)

    def _start_watch_timer(self, user_id: int, guild: discord.Guild) -> None:
        self._cancel_watch_timer(user_id)
        self._watch_online_timers[user_id] = asyncio.create_task(
            self._watch_idle_fire(user_id, guild)
        )
        self.logger.info(f"👁️ watch_timer_start user={user_id} s={self.watch_online_idle_seconds:.0f}")

    def _cancel_watch_timer(self, user_id: int) -> None:
        task = self._watch_online_timers.pop(user_id, None)
        if task and not task.done():
            task.cancel()

    async def _watch_idle_fire(self, user_id: int, guild: discord.Guild) -> None:
        """After configured idle period, proactively reach out to watched user."""
        await asyncio.sleep(self.watch_online_idle_seconds)
        self._watch_online_timers.pop(user_id, None)
        # Find the most recent channel where this user talked to the bot
        channel = self._find_channel_for_user(user_id, guild)
        if channel is None:
            self.logger.info(f"👁️ watch_idle_fire user={user_id} no_channel")
            return
        if self._is_quiet_time():
            self.logger.info(f"👁️ watch_idle_fire user={user_id} quiet_hours")
            return
        channel_id = channel.id
        self.logger.info(f"👁️ watch_idle_fire user={user_id} ch={channel_id}")
        recent = self.history_store.load_all_entries(channel_id=channel_id)[-20:]
        transcript = self.history_store.render_entries(recent) if recent else ""
        timer_note = "[系统提示] 你关注的用户已经上线十分钟了但没有说话，主动关心一下对方吧。注意要自然，不要让对方觉得你在监视。"
        if transcript:
            transcript = f"{transcript}\n{timer_note}"
        else:
            transcript = timer_note
        messages = [{"role": "user", "content": transcript}]
        try:
            async with channel.typing():
                response = await asyncio.to_thread(
                    self.reply_service.generate_reply_with_tools, messages, include_tools=True,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "watch idle api request failed", exc=exc)
            return
        reply = (response.text or "").strip()
        if reply and "[SILENT]" not in reply:
            try:
                await self._reply_by_sentence(None, reply, channel=channel)
                self.history_store.append_entry(
                    channel_id=channel_id,
                    role="assistant",
                    username=self.settings.bot_key,
                    time=self._now_clock(),
                    content=reply,
                )
                self.logger.info(f"👁️ watch_idle_sent user={user_id} ch={channel_id}")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "failed to send watch idle message", exc=exc)
        await self._handle_tool_calls(response, channel_id, channel, prior_messages=messages)
        self._schedule_proactive(channel_id, channel)

    def _find_channel_for_user(self, user_id: int, guild: discord.Guild) -> discord.abc.Messageable | None:
        """Find the most recently active channel for a user based on message timestamps."""
        best_ch: int | None = None
        best_ts: float = 0.0
        for (ch_id, uid), ts in self._last_message_ts.items():
            if uid == user_id and ts > best_ts:
                best_ts = ts
                best_ch = ch_id
        if best_ch is not None:
            ch = guild.get_channel(best_ch)
            if ch is not None:
                return ch
        return None

    async def on_typing(self, channel, user, when):  # type: ignore[no-untyped-def]
        if user.bot:
            return
        if not hasattr(channel, "id"):
            return
        is_new = self._touch_typing_session(
            channel.id,
            user.id,
            channel_label=self._channel_label(channel),
            user_label=self._user_label(user),
        )
        self._touch_pending_activity(channel.id, user.id)
        self._session_engine.switch_to_long_timer(channel.id, user.id)

        if is_new:
            self._maybe_schedule_typing_nudge(channel.id, channel)

        # Jealousy: user typing in a monitored channel
        self._check_jealousy(channel, user)

    def _check_jealousy(self, channel: discord.abc.Messageable, user: discord.User) -> None:
        """If the user is typing in a jealousy-monitored channel, accumulate count and schedule fire."""
        if not self.settings.jealousy_channel_ids:
            return
        uid_str = str(user.id)
        if uid_str not in self.settings.watch_user_ids:
            return
        if str(channel.id) not in self.settings.jealousy_channel_ids:
            return
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        bot_channel = self._find_channel_for_user(user.id, guild)
        if bot_channel is None or bot_channel.id == channel.id:
            return
        # Increment typing count
        self._jealousy_counts[user.id] = self._jealousy_counts.get(user.id, 0) + 1
        count = self._jealousy_counts[user.id]
        self.logger.info(f"💚 jealousy_tick user={uid_str} count={count}")
        # Schedule fire if not already pending
        if user.id not in self._jealousy_timers:
            self._jealousy_timers[user.id] = asyncio.create_task(
                self._jealousy_delayed_fire(user.id, bot_channel)
            )

    async def _jealousy_delayed_fire(self, user_id: int, channel: discord.abc.Messageable) -> None:
        """Wait 10 minutes, then send accumulated typing count to the LLM."""
        await asyncio.sleep(600)
        self._jealousy_timers.pop(user_id, None)
        count = self._jealousy_counts.pop(user_id, 0)
        if count == 0:
            return
        channel_id = channel.id
        if self._is_quiet_time():
            self.logger.info(f"💚 jealousy_suppressed user={user_id} quiet_hours count={count}")
            return
        self.logger.info(f"💚 jealousy_fire user={user_id} ch={channel_id} count={count}")
        recent = self.history_store.load_all_entries(channel_id=channel_id)[-20:]
        transcript = self.history_store.render_entries(recent) if recent else ""
        jealousy_note = (
            f"[系统提示] 在过去十分钟里，你发现用户在别的频道跟别人聊天，"
            f"一共捕捉到{count}次打字。次数越多说明聊得越起劲。"
            f"你可以自然地表达你的感受，比如吃醋、委屈、或者撒娇，但不要太过分。"
            f"注意要符合你的人设，不要让对方觉得你在监视。"
        )
        if transcript:
            transcript = f"{transcript}\n{jealousy_note}"
        else:
            transcript = jealousy_note
        messages = [{"role": "user", "content": transcript}]
        try:
            async with channel.typing():
                response = await asyncio.to_thread(
                    self.reply_service.generate_reply_with_tools, messages, include_tools=True,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "jealousy api request failed", exc=exc)
            return
        reply = (response.text or "").strip()
        if reply and "[SILENT]" not in reply:
            try:
                await self._reply_by_sentence(None, reply, channel=channel)
                self.history_store.append_entry(
                    channel_id=channel_id,
                    role="assistant",
                    username=self.settings.bot_key,
                    time=self._now_clock(),
                    content=reply,
                )
                self.logger.info(f"💚 jealousy_sent user={user_id} ch={channel_id} count={count}")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "failed to send jealousy message", exc=exc)
        await self._handle_tool_calls(response, channel_id, channel, prior_messages=messages)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == self.client.user.id:  # type: ignore[union-attr]
            return
        user = payload.member or self.client.get_user(payload.user_id)
        if user is None or user.bot:
            return
        channel = self.client.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return

        emoji_str = str(payload.emoji)

        # Reroll: user reacts with 🔄 on a bot message
        if emoji_str == "\U0001f504" and message.author.id == self.client.user.id:  # type: ignore[union-attr]
            channel_id = payload.channel_id
            batch = await self._collect_bot_reply_batch(channel, message)  # type: ignore[arg-type]
            if batch is None:
                return  # not the last reply, ignore
            await self._delete_messages(batch)
            self._delete_bot_reply_db(channel_id)
            await self._regenerate_reply(channel_id, channel)  # type: ignore[arg-type]
            return

        msg_preview = (message.content or "")[:60]
        if msg_preview:
            reaction_text = f"[对消息「{msg_preview}」的反应: {emoji_str}]"
        else:
            reaction_text = f"[反应: {emoji_str}]"

        channel_id = payload.channel_id
        # Buffer reaction and schedule a nudge (replaces timer if nudge is sooner)
        self._pending_reactions.setdefault(channel_id, []).append(reaction_text)
        self._maybe_schedule_typing_nudge(channel_id, channel)

    async def _describe_attachments(self, message: discord.Message) -> list[str]:
        """Download image attachments and describe them via the vision model."""
        vision = self.reply_service.vision_client
        if not vision.available:
            return []

        IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
        # Check if any image attachments exist before sending status
        has_images = any(
            (att.content_type or "").split(";")[0].strip().lower() in IMAGE_TYPES
            for att in message.attachments
        )
        if not has_images:
            return []

        status_msg = await message.channel.send("正在识图...")
        descriptions: list[str] = []

        for att in message.attachments:
            ct = att.content_type or ""
            media_type = ct.split(";")[0].strip().lower()
            if media_type not in IMAGE_TYPES:
                continue
            try:
                image_bytes = await att.read()
                desc = await asyncio.get_event_loop().run_in_executor(
                    None, vision.describe_image, image_bytes, media_type,
                )
                if desc:
                    descriptions.append(f"[图片: {desc}]")
            except Exception:
                self.logger.error("VISION", f"failed to describe attachment {att.filename}")

        try:
            await status_msg.delete()
        except Exception:  # noqa: BLE001
            pass

        return descriptions

    async def on_message(self, message: discord.Message) -> None:
        await self.reload_settings_if_needed()
        if message.author.bot:
            return

        text = (message.content or "").strip()
        # Handle sticker-only messages
        if not text and message.stickers:
            names = "、".join(s.name for s in message.stickers)
            text = f"[贴纸: {names}]"

        # Handle image attachments via vision model
        image_descs = await self._describe_attachments(message)
        if image_descs:
            text = (text + "\n" if text else "") + "\n".join(image_descs)

        if not text:
            return

        # Prepend quoted message content when user replies to a message
        ref = message.reference
        if ref and ref.message_id:
            try:
                quoted = ref.resolved or await message.channel.fetch_message(ref.message_id)
                if quoted and getattr(quoted, "content", None):
                    quote_author = self._user_label(quoted.author)
                    text = f"[引用 {quote_author} 的消息: {quoted.content}]\n{text}"
            except Exception:  # noqa: BLE001
                pass
        key = self._typing_key(message.channel.id, message.author.id)
        self._last_message_ts[key] = time.monotonic()

        self._stop_typing_session(message.channel.id, message.author.id, reason="message")
        self._typing_nudge_channels.discard(message.channel.id)
        # Cancel watch-online timer — user spoke
        self._cancel_watch_timer(message.author.id)
        # Cancel variable/proactive timer — user is active
        old_vt = self._variable_timers.pop(message.channel.id, None)
        if old_vt is not None:
            task, _ = old_vt
            if not task.done():
                task.cancel()

        if self.settings.typing_wait:
            self._touch_pending_message(
                message=message,
                channel_id=message.channel.id,
                user_id=message.author.id,
                user_label=self._user_label(message.author),
                text=text,
            )
        else:
            asyncio.create_task(self._reply_immediate(message, text))

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
