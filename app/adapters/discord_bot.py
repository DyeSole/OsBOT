from __future__ import annotations

import asyncio
import json as _json
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


LOGIN_TARGETS: dict[str, tuple[str, str]] = {
    "bilibili": ("bilibili", "https://passport.bilibili.com/login"),
    "b站": ("bilibili", "https://passport.bilibili.com/login"),
    "哔哩哔哩": ("bilibili", "https://passport.bilibili.com/login"),
    "xiaohongshu": ("xiaohongshu", "https://www.xiaohongshu.com"),
    "小红书": ("xiaohongshu", "https://www.xiaohongshu.com"),
    "rednote": ("xiaohongshu", "https://www.xiaohongshu.com"),
}


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
        self._last_msg_ts_path = BASE_DIR / "data" / "last_message_ts.json"
        self._load_last_message_ts()
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

    async def handle_browser_login(self, interaction: discord.Interaction, *, app: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            import io
            from app.infra.browser_client import (
                close_login_session,
                finish_login_session,
                start_login_session,
            )

            profile, url, source = await self._resolve_login_target(app)
            session = await start_login_session(profile, url)
            file = discord.File(
                io.BytesIO(session["preview"]),
                filename=f"{profile}-login-preview.png",
            )
            initial_text = "\n".join(
                [
                    "浏览器登录",
                    "",
                    f"应用: {app}",
                    f"登录页: {url}",
                    f"来源: {source}",
                    "截图已返回，请在 120 秒内完成扫码/登录。",
                    "我会每 5 秒检查一次，成功后立即保存登录态。",
                ]
            )
            await interaction.edit_original_response(
                content=initial_text,
                attachments=[file],
            )
            try:
                msg = await finish_login_session(
                    session,
                    profile,
                    login_url=url,
                    timeout_ms=120_000,
                    poll_interval_ms=5_000,
                )
            finally:
                await close_login_session(session)
            await interaction.edit_original_response(
                content=f"{initial_text}\n\n结果: {msg}",
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("BROWSER", "login save failed", exc=exc)
            await interaction.edit_original_response(
                content=f"浏览器登录\n\n保存登录态失败: {exc}",
            )

    @staticmethod
    def browser_profiles_text() -> str:
        from app.infra.browser_client import list_profiles

        profiles = list_profiles()
        if profiles:
            return "社交平台\n\n已保存的登录态:\n" + "\n".join(f"  • {p}" for p in profiles)
        return "社交平台\n\n还没有保存任何登录态。"

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

    @staticmethod
    def _slugify_profile(value: str) -> str:
        profile = re.sub(r"[^\w.-]+", "_", value.strip().lower(), flags=re.UNICODE).strip("._")
        return profile or "browser"

    @staticmethod
    def _pick_login_url(results: list[dict[str, str]]) -> str:
        for item in results:
            href = str(item.get("href", "")).strip()
            if href.startswith(("http://", "https://")):
                return href
        return ""

    async def _resolve_login_target(self, app: str) -> tuple[str, str, str]:
        app_name = app.strip()
        if not app_name:
            raise ValueError("应用名称不能为空")

        preset = LOGIN_TARGETS.get(app_name.lower())
        if preset is None:
            preset = LOGIN_TARGETS.get(app_name)
        if preset is not None:
            profile, url = preset
            return profile, url, "preset"

        from app.infra.search_client import web_search

        query = f"{app_name} 官方登录页"
        results = await asyncio.to_thread(
            web_search,
            query,
            max_results=5,
            base_url=self.settings.search_base_url,
            api_key=self.settings.search_api_key,
            model=self.settings.search_model,
        )
        url = self._pick_login_url(results)
        if not url:
            raise RuntimeError(f"未找到 {app_name} 的登录页，请后续补充预设站点。")
        return self._slugify_profile(app_name), url, "search"

    async def reload_settings_if_needed(self) -> bool:
        current_mtime = env_last_modified()
        if current_mtime <= self._env_mtime:
            return False

        settings = load_settings()
        self.apply_settings(settings)
        self._env_mtime = current_mtime
        self.logger.info("env hot-reloaded from .env")
        return True

    async def _build_context_for_api(
        self,
        *,
        channel_id: int,
        pending_messages: list[dict[str, str]],
    ) -> str:
        live_block = self.context_builder.build_live_block(channel_id=channel_id)
        estimated_tokens = self.context_builder.estimate_tokens(live_block)
        if self.settings.app_mode == "debug":
            self.logger.info(
                f"🧮 transcript_tokens ch={channel_id} est_tokens={estimated_tokens} "
                f"limit={self.settings.transcript_max_tokens}"
            )
        self.reply_service.set_debug_context_meta(
            estimated_tokens=estimated_tokens,
            limit=self.settings.transcript_max_tokens,
        )
        transcript = self.context_builder.build_context_for_api(
            channel_id=channel_id,
            pending_messages=pending_messages,
        )
        if estimated_tokens <= self.settings.transcript_max_tokens:
            return transcript

        self.logger.info(
            f"🗜️ transcript_over_limit ch={channel_id} est_tokens={estimated_tokens} "
            f"limit={self.settings.transcript_max_tokens} -> compress"
        )
        try:
            await asyncio.to_thread(
                self.compression_service.compress_history,
                channel_id=channel_id,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "auto compression failed", exc=exc)
            return transcript

        return self.context_builder.build_context_for_api(
            channel_id=channel_id,
            pending_messages=pending_messages,
        )

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
    ) -> tuple[LLMResponse, list[discord.Message]]:
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
        sent_msgs: list[discord.Message] = []
        is_novel = self.settings.split_mode == "novel"
        novel_msg: discord.Message | None = None
        novel_full = ""
        novel_last_edit = 0.0

        while True:
            try:
                kind, value = q.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.05)
                continue

            if kind == "text":
                buffer += value
                if is_novel:
                    novel_full += value
                    now = asyncio.get_event_loop().time()
                    if now - novel_last_edit >= 1.0:
                        display = novel_full.strip()
                        if display:
                            try:
                                if novel_msg is None:
                                    if anchor_message is not None:
                                        novel_msg = await anchor_message.reply(
                                            display, mention_author=False,
                                            allowed_mentions=AllowedMentions.none(),
                                        )
                                    else:
                                        novel_msg = await channel.send(
                                            display, allowed_mentions=AllowedMentions.none(),
                                        )
                                    sent_msgs.append(novel_msg)
                                elif len(display) <= 2000:
                                    await novel_msg.edit(content=display)
                                else:
                                    novel_full = value
                                    novel_msg = await channel.send(
                                        value.strip() or "...",
                                        allowed_mentions=AllowedMentions.none(),
                                    )
                                    sent_msgs.append(novel_msg)
                            except Exception:  # noqa: BLE001
                                pass
                            novel_last_edit = now
                else:
                    parts = self._split_sentences(buffer)
                    if len(parts) > 1:
                        delay = self.settings.chat_reply_delay_seconds
                        for s in parts[:-1]:
                            if not is_first and delay > 0:
                                async with channel.typing():
                                    await asyncio.sleep(delay)
                            if is_first and anchor_message is not None:
                                msg = await anchor_message.reply(
                                    s, mention_author=False,
                                    allowed_mentions=AllowedMentions.none(),
                                )
                            else:
                                msg = await channel.send(
                                    s, allowed_mentions=AllowedMentions.none(),
                                )
                            sent_msgs.append(msg)
                            is_first = False
                        buffer = parts[-1]
            elif kind == "done":
                if is_novel:
                    display = novel_full.strip()
                    if display:
                        try:
                            if novel_msg is None:
                                if anchor_message is not None:
                                    novel_msg = await anchor_message.reply(
                                        display, mention_author=False,
                                        allowed_mentions=AllowedMentions.none(),
                                    )
                                else:
                                    novel_msg = await channel.send(
                                        display, allowed_mentions=AllowedMentions.none(),
                                    )
                                sent_msgs.append(novel_msg)
                            elif len(display) <= 2000:
                                await novel_msg.edit(content=display)
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    if buffer.strip():
                        if not is_first and self.settings.chat_reply_delay_seconds > 0:
                            async with channel.typing():
                                await asyncio.sleep(self.settings.chat_reply_delay_seconds)
                        if is_first and anchor_message is not None:
                            msg = await anchor_message.reply(
                                buffer.strip(), mention_author=False,
                                allowed_mentions=AllowedMentions.none(),
                            )
                        else:
                            msg = await channel.send(
                                buffer.strip(), allowed_mentions=AllowedMentions.none(),
                            )
                        sent_msgs.append(msg)
                return value, sent_msgs  # type: ignore[return-value]
            elif kind == "error":
                raise value  # type: ignore[misc]

        return LLMResponse(text=""), sent_msgs  # unreachable

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
                since_last_msg = f" since_last_msg={time.time() - last_msg_ts:.2f}s"
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
        transcript = await self._build_context_for_api(
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
            response, sent_msgs = await self._stream_and_send(
                pending.anchor_message, pending.channel, messages,
            )
            reply = (response.text or "").strip()
            has_search = any(tc.name == "web_search" for tc in response.tool_calls)
            if reply and not has_search:
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
            edit_msg = sent_msgs[-1] if sent_msgs and has_search else None
            await self._handle_tool_calls(
                response,
                channel_id,
                pending.channel,
                prior_messages=messages,
                edit_msg=edit_msg,
                had_reply=bool(reply),
                source_message=pending.anchor_message,
            )
            self._schedule_proactive(channel_id, pending.channel)
        except Exception as exc:  # noqa: BLE001
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
        channel_id = message.channel.id
        user_label = self._user_label(message.author)
        now_clock = self._now_clock()

        pending_entry = {
            "role": "user",
            "username": user_label,
            "time": now_clock,
            "content": text,
        }
        transcript = await self._build_context_for_api(
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
            response, sent_msgs = await self._stream_and_send(
                message, message.channel, messages,
            )
            reply = (response.text or "").strip()
            has_search = any(tc.name == "web_search" for tc in response.tool_calls)
            if reply and not has_search:
                self.history_store.append_entry(
                    channel_id=channel_id,
                    role="assistant",
                    username=self.settings.bot_key,
                    time=self._now_clock(),
                    content=reply,
                )
            edit_msg = sent_msgs[-1] if sent_msgs and has_search else None
            await self._handle_tool_calls(
                response,
                channel_id,
                message.channel,
                prior_messages=messages,
                edit_msg=edit_msg,
                had_reply=bool(reply),
                source_message=message,
            )
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

    def _process_timer_calls(
        self,
        tool_calls: list,
        channel_id: int,
        channel: discord.abc.Messageable,
    ) -> list[tuple[float, str | None]]:
        alarms_set: list[tuple[float, str | None]] = []
        for tc in tool_calls:
            if tc.name == "set_timer":
                seconds = tc.input.get("seconds", 0)
                reason = tc.input.get("reason") or None
                if isinstance(seconds, (int, float)) and seconds > 0:
                    if reason:
                        self._schedule_alarm(channel_id, channel, seconds, reason)
                        alarms_set.append((seconds, reason))
                    else:
                        self._schedule_variable_timer(channel_id, channel, seconds, source="llm")
        return alarms_set

    async def _handle_tool_calls(
        self,
        response,
        channel_id: int,
        channel: discord.abc.Messageable,
        *,
        prior_messages: list[dict[str, str]] | None = None,
        search_depth: int = 0,
        edit_msg: discord.Message | None = None,
        had_reply: bool = True,
        source_message: discord.Message | None = None,
    ) -> None:
        alarms_set = self._process_timer_calls(response.tool_calls, channel_id, channel)
        if alarms_set and not had_reply:
            for seconds, reason in alarms_set:
                mins = seconds / 60
                if mins >= 1:
                    time_str = f"{mins:.0f}分钟"
                else:
                    time_str = f"{seconds:.0f}秒"
                confirm = f"⏰ 好的，{time_str}后提醒你：{reason}" if reason else f"⏰ 好的，{time_str}后提醒你"
                try:
                    await channel.send(confirm, allowed_mentions=AllowedMentions.none())
                    self.history_store.append_entry(
                        channel_id=channel_id,
                        role="assistant",
                        username=self.settings.bot_key,
                        time=self._now_clock(),
                        content=confirm,
                    )
                except Exception:  # noqa: BLE001
                    pass
        for tc in response.tool_calls:
            if tc.name == "add_reaction":
                emoji = str(tc.input.get("emoji", "")).strip()
                if emoji and source_message is not None:
                    try:
                        reaction = (
                            discord.PartialEmoji.from_str(emoji)
                            if emoji.startswith("<") and emoji.endswith(">")
                            else emoji
                        )
                        await source_message.add_reaction(reaction)
                    except Exception as exc:  # noqa: BLE001
                        self.logger.error("API", f"failed to add reaction: {emoji}", exc=exc)
            if tc.name == "web_search":
                query = tc.input.get("query", "")
                if query:
                    if search_depth >= 3:
                        self.logger.info(f"🔍 search_depth_limit query={query} depth={search_depth}")
                    else:
                        await self._execute_search(query, channel_id, channel, prior_messages, search_depth=search_depth, edit_msg=edit_msg)

    async def _execute_search(
        self,
        query: str,
        channel_id: int,
        channel: discord.abc.Messageable,
        prior_messages: list[dict[str, str]] | None = None,
        *,
        search_depth: int = 0,
        edit_msg: discord.Message | None = None,
    ) -> None:
        from app.infra.search_client import web_search
        from app.services.reply_service import load_system_prompt

        self.logger.info(f"🔍 web_search query={query} depth={search_depth}")
        recent_entries = self.history_store.load_all_entries(channel_id=channel_id)
        context_hint = self.history_store.render_entries(recent_entries[-10:]) if recent_entries else ""
        soul = load_system_prompt()
        try:
            results = await asyncio.to_thread(
                web_search,
                query,
                base_url=self.settings.search_base_url,
                api_key=self.settings.search_api_key,
                model=self.settings.search_model,
                context=context_hint,
                soul=soul,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "web search failed", exc=exc)
            results = []

        if results:
            from urllib.parse import urlparse
            lines = [f"[搜索结果: {query}]"]
            for r in results:
                source = urlparse(r['href']).netloc.removeprefix("www.") if r['href'] else "unknown"
                lines.append(f"- [{source}] {r['title']}\n  {r['body']}\n  {r['href']}")
            search_block = "\n".join(lines)
            search_block += "\n\n请根据以上搜索结果回答用户，引用相关来源。如果多个来源有不同说法，请分别说明。"
        else:
            search_block = f"[搜索结果: {query}]\n未找到相关结果。"

        if search_depth == 0:
            recent = recent_entries[-self.settings.context_entries:]
            recent_block = self.history_store.render_entries(recent) if recent else ""
            context_parts = [p for p in [recent_block, search_block] if p]
            messages = [{"role": "user", "content": "\n\n".join(context_parts)}]
        else:
            messages = list(prior_messages) if prior_messages else []
            messages.append({"role": "user", "content": search_block})

        next_depth = search_depth + 1
        wants_more = next_depth < 3
        try:
            async with channel.typing():
                search_response = await asyncio.to_thread(
                    self.reply_service.generate_reply_with_tools, messages, include_tools=True, include_search=wants_more,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "search follow-up api request failed", exc=exc)
            return

        next_search = next(
            (tc for tc in search_response.tool_calls if tc.name == "web_search" and tc.input.get("query")),
            None,
        )
        if next_search and next_depth < 3:
            self.logger.info(f"🔍 search_continue depth={next_depth} next_query={next_search.input['query']}")
            intermediate = (search_response.text or "").strip()
            if intermediate and edit_msg:
                try:
                    existing = edit_msg.content or ""
                    combined = f"{existing}\n{intermediate}" if existing else intermediate
                    if len(combined) <= 2000:
                        await edit_msg.edit(content=combined)
                    else:
                        await self._reply_by_sentence(None, intermediate, channel=channel)
                except Exception:  # noqa: BLE001
                    pass
            self._process_timer_calls(search_response.tool_calls, channel_id, channel)
            await self._execute_search(
                next_search.input["query"], channel_id, channel, messages,
                search_depth=next_depth, edit_msg=edit_msg,
            )
            return

        reply = (search_response.text or "").strip()
        if reply:
            try:
                if edit_msg:
                    existing = edit_msg.content or ""
                    combined = f"{existing}\n{reply}" if existing else reply
                    if len(combined) <= 2000:
                        await edit_msg.edit(content=combined)
                    else:
                        await self._reply_by_sentence(None, reply, channel=channel)
                else:
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

        await self._handle_tool_calls(search_response, channel_id, channel, prior_messages=messages, search_depth=next_depth, edit_msg=edit_msg)

    def _schedule_variable_timer(
        self,
        channel_id: int,
        channel: discord.abc.Messageable,
        seconds: float,
        *,
        source: str = "auto",
    ) -> None:
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
        await asyncio.sleep(seconds)
        self._variable_timers.pop(channel_id, None)
        if self.settings.jealousy_channel_ids and str(channel_id) in self.settings.jealousy_channel_ids:
            self._log_typing(f"💚 timer_skip_jealousy ch={channel_id}")
            return
        if self._is_quiet_time():
            self._quiet_channels.setdefault(channel_id, channel)
            self._schedule_quiet_flush()
            self._log_typing(f"🤫 timer_quiet ch={channel_id} s={seconds}")
            return

        recent = self.history_store.load_all_entries(channel_id=channel_id)[-self.settings.context_entries:]
        transcript = self.history_store.render_entries(recent) if recent else ""
        is_typing_nudge = channel_id in self._typing_nudge_channels
        self._typing_nudge_channels.discard(channel_id)
        if is_typing_nudge:
            timer_note = "[系统提示] ta刚才在打字，但最终没有发出消息。"
        elif seconds != self.proactive_idle_seconds:
            timer_note = f"[system: your set_timer for {seconds}s has expired]\n{self.prompt_service.read_prompt('proactive')}"
        else:
            timer_note = f"[系统提示] {self.prompt_service.read_prompt('proactive')}"
        pending_reasons = self._pending_alarm_reasons.pop(channel_id, [])
        if pending_reasons:
            alarm_lines = "\n".join(f"- {r}" for r in pending_reasons)
            timer_note += (
                f"\n[system: 以下闹钟已到期，你必须提醒用户这些事情，不可以沉默]\n{alarm_lines}"
            )
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

        await self._handle_tool_calls(response, channel_id, channel, prior_messages=messages)

    async def _alarm_fire(
        self,
        channel_id: int,
        channel: discord.abc.Messageable,
        seconds: float,
        reason: str,
    ) -> None:
        task = asyncio.current_task()
        try:
            await asyncio.sleep(seconds)
        finally:
            alarm_list = self._alarms.get(channel_id, [])
            if task in alarm_list:
                alarm_list.remove(task)
            if not alarm_list:
                self._alarms.pop(channel_id, None)

        if self._is_quiet_time():
            self._quiet_buffered_reasons.setdefault(channel_id, []).append(reason)
            self._quiet_channels[channel_id] = channel
            self._schedule_quiet_flush()
            self.logger.info(f"🤫 alarm_buffered_quiet channel={channel_id} reason={reason}")
            return

        vt = self._variable_timers.get(channel_id)
        if vt is not None:
            _, deadline = vt
            remaining = deadline - time.monotonic()
            if 0 < remaining < 30:
                self._pending_alarm_reasons.setdefault(channel_id, []).append(reason)
                self.logger.info(f"⏰ alarm_buffered channel={channel_id} reason={reason} remaining={remaining:.0f}s")
                return

        recent = self.history_store.load_all_entries(channel_id=channel_id)[-self.settings.context_entries:]
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
        self._typing_nudge_channels.discard(channel_id)
        if self.proactive_idle_seconds <= 0:
            return
        self._schedule_variable_timer(channel_id, channel, self.proactive_idle_seconds)

    def _maybe_schedule_typing_nudge(
        self, channel_id: int, channel: discord.abc.Messageable,
    ) -> None:
        nudge = self.typing_nudge_seconds
        vt = self._variable_timers.get(channel_id)
        if vt is not None:
            _, deadline = vt
            remaining = deadline - time.monotonic()
            if remaining <= nudge:
                return
        self._typing_nudge_channels.add(channel_id)
        self._schedule_variable_timer(channel_id, channel, nudge, source="typing_nudge")

    @staticmethod
    def _parse_time(s: str) -> dt_time | None:
        s = s.strip()
        if not s:
            return None
        try:
            parts = s.split(":")
            return dt_time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return None

    def _is_quiet_time(self) -> bool:
        if not self.settings.quiet_enabled:
            return False
        start = self._parse_time(self.settings.quiet_start)
        end = self._parse_time(self.settings.quiet_end)
        if start is None or end is None:
            return False
        now = _now().time()
        if start <= end:
            return start <= now < end
        return now >= start or now < end

    def _seconds_until_quiet_end(self) -> float:
        end = self._parse_time(self.settings.quiet_end)
        if end is None:
            return 0.0
        now = _now()
        end_today = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        if end_today <= now:
            from datetime import timedelta
            end_today += timedelta(days=1)
        return (end_today - now).total_seconds()

    def _schedule_quiet_flush(self) -> None:
        if self._quiet_flush_task is not None and not self._quiet_flush_task.done():
            return
        wait = self._seconds_until_quiet_end()
        if wait <= 0:
            return
        self._quiet_flush_task = asyncio.create_task(self._quiet_flush_fire(wait))

    async def _quiet_flush_fire(self, wait_seconds: float) -> None:
        await asyncio.sleep(wait_seconds)
        buffered = dict(self._quiet_buffered_reasons)
        channels = dict(self._quiet_channels)
        self._quiet_buffered_reasons.clear()
        self._quiet_channels.clear()

        morning_prompt = self.prompt_service.read_prompt("morning")
        proactive_prompt = self.prompt_service.read_prompt("proactive")

        for channel_id, channel in channels.items():
            recent = self.history_store.load_all_entries(channel_id=channel_id)[-self.settings.context_entries:]
            history_block = self.history_store.render_entries(recent) if recent else ""

            parts: list[str] = []
            if history_block:
                parts.append(history_block)
            parts.append(f"[system: 静默时间已结束]\n{morning_prompt}")
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
        cid = channel.id
        self.logger.info(f"🗑️ channel_deleted id={cid} name={getattr(channel, 'name', '?')}")
        h = self.history_store.delete_channel(cid)
        m = self.compression_store.delete_channel(cid)
        if h or m:
            self.logger.info(f"🧹 cleaned channel_id={cid} history={h} memory={m}")

    async def _collect_bot_reply_batch(
        self,
        channel: discord.abc.Messageable,
        anchor: discord.Message,
    ) -> list[discord.Message] | None:
        bot_id = self.client.user.id  # type: ignore[union-attr]
        batch = [anchor]
        async for msg in channel.history(before=anchor, limit=50):  # type: ignore[union-attr]
            if msg.author.id == bot_id:
                batch.append(msg)
            else:
                break
        async for msg in channel.history(after=anchor, limit=50):  # type: ignore[union-attr]
            if msg.author.id == bot_id:
                batch.append(msg)
            else:
                return None
        return batch

    async def _delete_messages(self, messages: list[discord.Message]) -> None:
        if not messages:
            return
        channel = messages[0].channel
        if hasattr(channel, "delete_messages") and len(messages) > 1:
            try:
                await channel.delete_messages(messages)  # type: ignore[union-attr]
                return
            except Exception:  # noqa: BLE001
                pass
        for msg in messages:
            try:
                await msg.delete()
            except Exception:  # noqa: BLE001
                pass

    def _delete_bot_reply_db(self, channel_id: int) -> None:
        self.history_store.pop_last_by_role(channel_id=channel_id, role="assistant")

    async def _regenerate_reply(
        self,
        channel_id: int,
        channel: discord.abc.Messageable,
    ) -> None:
        transcript = await self._build_context_for_api(
            channel_id=channel_id,
            pending_messages=[],
        )
        if not transcript:
            return
        messages = [{"role": "user", "content": transcript}]
        try:
            async with channel.typing():
                response, sent_msgs = await self._stream_and_send(
                    None, channel, messages,
                )
            reply = (response.text or "").strip()
            has_search = any(tc.name == "web_search" for tc in response.tool_calls)
            if reply and not has_search:
                self.history_store.append_entry(
                    channel_id=channel_id,
                    role="assistant",
                    username=self.settings.bot_key,
                    time=self._now_clock(),
                    content=reply,
                )
            edit_msg = sent_msgs[-1] if sent_msgs and has_search else None
            await self._handle_tool_calls(response, channel_id, channel, prior_messages=messages, edit_msg=edit_msg)
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
        if entries[-1].get("role") != "assistant":
            return
        old_text = (before.content or "").strip()
        for e in reversed(entries):
            if e["role"] == "user":
                if old_text and e["content"] != old_text:
                    return
                break
        else:
            return

        to_delete: list[discord.Message] = []
        bot_id = self.client.user.id  # type: ignore[union-attr]
        async for msg in after.channel.history(after=after, limit=50):
            if msg.author.id == bot_id:
                to_delete.append(msg)
            else:
                break
        await self._delete_messages(to_delete)

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
        if prev is None:
            return
        if old_status == "offline" and new_status != "offline":
            self._start_watch_timer(after.id, after.guild)
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
        await asyncio.sleep(self.watch_online_idle_seconds)
        self._watch_online_timers.pop(user_id, None)
        channel = self._find_channel_for_user(user_id, guild)
        if channel is None:
            self.logger.info(f"👁️ watch_idle_fire user={user_id} no_channel")
            return
        if self._is_quiet_time():
            self.logger.info(f"👁️ watch_idle_fire user={user_id} quiet_hours")
            return
        channel_id = channel.id
        self.logger.info(f"👁️ watch_idle_fire user={user_id} ch={channel_id}")
        recent = self.history_store.load_all_entries(channel_id=channel_id)[-self.settings.context_entries:]
        transcript = self.history_store.render_entries(recent) if recent else ""
        minutes = int(self.watch_online_idle_seconds // 60) or 1
        raw_prompt = self.prompt_service.read_prompt("watch_online")
        if not raw_prompt.strip():
            raw_prompt = "[系统提示] 你关注的用户已经上线{minutes}分钟了但没有说话，跟他主动说句话。"
        timer_note = raw_prompt.strip().replace("{minutes}", str(minutes))
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

    def _load_last_message_ts(self) -> None:
        try:
            if self._last_msg_ts_path.exists():
                raw = _json.loads(self._last_msg_ts_path.read_text(encoding="utf-8"))
                for key_str, ts in raw.items():
                    ch_id_s, uid_s = key_str.split(":", 1)
                    self._last_message_ts[(int(ch_id_s), int(uid_s))] = float(ts)
        except Exception:  # noqa: BLE001
            pass  # corrupted file — start fresh

    def _save_last_message_ts(self) -> None:
        self._last_msg_ts_path.parent.mkdir(parents=True, exist_ok=True)
        data = {f"{ch_id}:{uid}": ts for (ch_id, uid), ts in self._last_message_ts.items()}
        self._last_msg_ts_path.write_text(_json.dumps(data), encoding="utf-8")

    def _find_channel_for_user(self, user_id: int, guild: discord.Guild) -> discord.abc.Messageable | None:
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

    def _check_jealousy(self, channel: discord.abc.Messageable, user: discord.User) -> None:
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
        if bot_channel is None:
            return
        if bot_channel.id == channel.id:
            return
        self._jealousy_counts[user.id] = self._jealousy_counts.get(user.id, 0) + 1
        count = self._jealousy_counts[user.id]
        self.logger.info(f"💚 jealousy_tick user={uid_str} msg_count={count}")
        if user.id not in self._jealousy_timers:
            self._jealousy_timers[user.id] = asyncio.create_task(
                self._jealousy_delayed_fire(user.id, bot_channel)
            )

    async def _jealousy_delayed_fire(self, user_id: int, channel: discord.abc.Messageable) -> None:
        await asyncio.sleep(30)
        self._jealousy_timers.pop(user_id, None)
        count = self._jealousy_counts.pop(user_id, 0)
        if count == 0:
            return
        channel_id = channel.id
        is_quiet = self._is_quiet_time()
        self.logger.info(f"💚 jealousy_fire user={user_id} ch={channel_id} msg_count={count} quiet={is_quiet}")
        recent = self.history_store.load_all_entries(channel_id=channel_id)[-self.settings.context_entries:]
        transcript = self.history_store.render_entries(recent) if recent else ""
        if is_quiet:
            jealousy_note = (
                f"[系统提示] 现在是安静时间段，ta不让你找ta，但ta自己跑去和情敌聊天了，"
                f"一共发了{count}条消息。次数越多说明聊得越起劲。"
                f"你可以自然地表达你的感受，比如吃醋、委屈、或者撒娇，但不要太过分。"
                f"注意要符合你的人设，不要让对方觉得你在监视。"
            )
        else:
            jealousy_note = (
                f"[系统提示] 在过去十分钟里，ta正在和情敌聊天，"
                f"一共发了{count}条消息。次数越多说明聊得越起劲。"
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
                self.logger.info(
                    f"🏷️ jealousy_sent user={user_id} ch={channel_id} msg_count={count}"
                    f" | prompt={jealousy_note}"
                    f" | reply={reply}"
                )
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

        if emoji_str == "\U0001f504" and message.author.id == self.client.user.id:  # type: ignore[union-attr]
            channel_id = payload.channel_id
            batch = await self._collect_bot_reply_batch(channel, message)  # type: ignore[arg-type]
            if batch is None:
                return
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
        self._pending_reactions.setdefault(channel_id, []).append(reaction_text)
        self._maybe_schedule_typing_nudge(channel_id, channel)

    async def _describe_attachments(self, message: discord.Message) -> list[str]:
        vision = self.reply_service.vision_client
        if not vision.available:
            return []

        IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
        has_images = any(
            (att.content_type or "").split(";")[0].strip().lower() in IMAGE_TYPES
            for att in message.attachments
        )
        if not has_images:
            return []

        vision_prompt = self.settings.vision_prompt
        channel_id = message.channel.id
        recent = self.history_store.load_all_entries(channel_id=channel_id)[-self.settings.context_entries:]
        context = self.history_store.render_entries(recent) if recent else ""
        status_msg = await message.channel.send("正在识图...")
        descriptions: list[str] = []

        for att in message.attachments:
            ct = att.content_type or ""
            media_type = ct.split(";")[0].strip().lower()
            if media_type not in IMAGE_TYPES:
                continue
            try:
                image_bytes = await att.read()
                import functools
                desc = await asyncio.get_event_loop().run_in_executor(
                    None, functools.partial(vision.describe_image, image_bytes, media_type, system_prompt=vision_prompt, context=context),
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
        if not text and message.stickers:
            names = "、".join(s.name for s in message.stickers)
            text = f"[贴纸: {names}]"

        image_descs = await self._describe_attachments(message)
        if image_descs:
            text = (text + "\n" if text else "") + "\n".join(image_descs)

        if not text:
            return

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
        self._last_message_ts[key] = time.time()
        self._save_last_message_ts()

        self._stop_typing_session(message.channel.id, message.author.id, reason="message")
        self._typing_nudge_channels.discard(message.channel.id)
        self._check_jealousy(message.channel, message.author)
        if self.settings.jealousy_channel_ids and str(message.channel.id) in self.settings.jealousy_channel_ids:
            return
        self._cancel_watch_timer(message.author.id)
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
