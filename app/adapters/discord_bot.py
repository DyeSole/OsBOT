from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime

import discord
from discord import AllowedMentions, app_commands

from app.config.settings import BASE_DIR, Settings, env_last_modified, load_settings, read_env_values, update_env_values
from app.core.logging import BotLogger
from app.core.session_engine import SessionEngine
from app.infra.storage import ChatHistoryStore, CompressionStore
from app.services.compression_service import CompressionService
from app.services.context_builder import ContextBuilder
from app.services.proactive_service import ProactiveService, _load_proactive_prompt
from app.services.prompt_service import PromptService
from app.services.reply_service import ReplyService


@dataclass
class TypingSession:
    started_at: float
    last_seen_at: float
    channel_label: str
    user_label: str


class ApiConfigModal(discord.ui.Modal, title="编辑 API 配置"):
    def __init__(self, bot: "DiscordBot"):
        super().__init__()
        self.bot = bot
        env_values = read_env_values()
        current = bot.settings
        self.base_url = discord.ui.TextInput(
            label="BASE_URL",
            default=env_values.get("BASE_URL", current.base_url),
            required=True,
            max_length=400,
        )
        self.api_key = discord.ui.TextInput(
            label="API_KEY",
            default=env_values.get("API_KEY", current.api_key),
            required=True,
            max_length=400,
        )
        self.model = discord.ui.TextInput(
            label="MODEL",
            default=env_values.get("MODEL", current.model),
            required=True,
            max_length=120,
        )
        self.add_item(self.base_url)
        self.add_item(self.api_key)
        self.add_item(self.model)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        update_env_values(
            {
                "BASE_URL": self.base_url.value.strip(),
                "API_KEY": self.api_key.value.strip(),
                "MODEL": self.model.value.strip(),
            }
        )
        await interaction.response.edit_message(
            content="工具箱\n\nAPI 配置已写入 .env，文件监听会自动生效。",
            view=ToolboxView(self.bot),
        )


class PromptEditModal(discord.ui.Modal):
    def __init__(self, bot: "DiscordBot", *, target: str, title: str):
        super().__init__(title=title)
        self.bot = bot
        self.target = target
        current_text = bot.prompt_service.read_prompt(target)
        self.content = discord.ui.TextInput(
            label=title,
            default=current_text[:4000],
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.bot.prompt_service.write_prompt(
            target=self.target,
            content=self.content.value,
        )
        await interaction.response.edit_message(
            content="提示词工具箱\n\n提示词已保存，下一次调用会自动生效。",
            view=PromptToolboxView(self.bot),
        )


class PromptToolboxView(discord.ui.View):
    def __init__(self, bot: "DiscordBot"):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="编辑人格提示词", style=discord.ButtonStyle.primary)
    async def edit_soul(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            PromptEditModal(self.bot, target="soul", title="编辑人格提示词")
        )

    @discord.ui.button(label="编辑压缩提示词", style=discord.ButtonStyle.secondary)
    async def edit_compression(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            PromptEditModal(self.bot, target="compression", title="编辑压缩提示词")
        )

    @discord.ui.button(label="返回", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="工具箱",
            view=ToolboxView(self.bot),
        )


class ToolboxView(discord.ui.View):
    def __init__(self, bot: "DiscordBot"):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="API配置", style=discord.ButtonStyle.primary)
    async def api_config(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ApiConfigModal(self.bot))

    @discord.ui.button(label="提示词", style=discord.ButtonStyle.secondary)
    async def prompts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="提示词工具箱",
            view=PromptToolboxView(self.bot),
        )


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
        self.proactive_service = ProactiveService(
            idle_seconds=settings.proactive_idle_seconds,
            reply_service=self.reply_service,
            context_builder=self.context_builder,
            history_store=self.history_store,
        )
        self.session_timeout_seconds = settings.session_timeout_seconds
        self.typing_timeout_seconds = settings.session_timeout_seconds
        self.typing_detect_delay_seconds = settings.typing_detect_delay_seconds
        self.reset_timer_seconds = settings.reset_timer_seconds
        self._env_watch_task: asyncio.Task | None = None
        self._env_mtime = env_last_modified()
        self._typing_sessions: dict[tuple[int, int], TypingSession] = {}
        self._session_engine = SessionEngine()
        self._last_message_ts: dict[tuple[int, int], float] = {}
        self._typing_watchdog_task: asyncio.Task | None = None
        self._variable_timers: dict[int, asyncio.Task] = {}
        self._alarms: dict[int, list[asyncio.Task]] = {}  # per-channel, multiple allowed

        intents = discord.Intents.default()
        intents.message_content = True
        intents.typing = True
        self.client = discord.Client(intents=intents)
        self.tree = app_commands.CommandTree(self.client)
        self._commands_synced = False

        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self.client.event(self.on_typing)
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
        self.typing_timeout_seconds = settings.session_timeout_seconds
        self.typing_detect_delay_seconds = settings.typing_detect_delay_seconds
        self.reset_timer_seconds = settings.reset_timer_seconds
        self.proactive_service.update_idle_seconds(settings.proactive_idle_seconds)
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
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _typing_probe_enabled(self) -> bool:
        return self.settings.app_mode == "debug" and self.settings.show_interaction_logs

    def _log_typing(self, message: str) -> None:
        if self._typing_probe_enabled():
            self.logger.info(message)

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        stripped = (text or "").strip()
        if not stripped:
            return []
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
        sentences = self._split_sentences(reply)
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
            async with pending.channel.typing():
                response = await asyncio.to_thread(self.reply_service.generate_reply_with_tools, messages)
            reply = response.text
            if reply:
                try:
                    await self._reply_by_sentence(pending.anchor_message, reply)
                except Exception as exc:
                    self.logger.error("LOGIC", f"reply() failed: {exc}")
                    try:
                        await self._reply_by_sentence(pending.anchor_message, reply)
                    except Exception as exc2:
                        self.logger.error("LOGIC", f"reply() fallback failed: {exc2}")
                        await pending.channel.send(
                            reply,
                            allowed_mentions=AllowedMentions.none(),
                        )
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
            self._handle_tool_calls(response, channel_id, pending.channel)
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

    def _handle_tool_calls(self, response, channel_id: int, channel: discord.abc.Messageable) -> None:
        """Process tool calls returned by the LLM."""
        for tc in response.tool_calls:
            if tc.name == "set_timer":
                seconds = tc.input.get("seconds", 0)
                reason = tc.input.get("reason") or None
                if isinstance(seconds, (int, float)) and seconds > 0:
                    if reason:
                        self._schedule_alarm(channel_id, channel, seconds, reason)
                    else:
                        self._schedule_variable_timer(channel_id, channel, seconds)
                    self.logger.info(f"⏰ timer_set channel={channel_id} seconds={seconds} reason={reason}")

    def _schedule_variable_timer(
        self,
        channel_id: int,
        channel: discord.abc.Messageable,
        seconds: float,
    ) -> None:
        """Schedule a variable timer, replacing the 5-min proactive timer."""
        # Cancel any existing variable timer for this channel
        old_task = self._variable_timers.pop(channel_id, None)
        if old_task is not None and not old_task.done():
            old_task.cancel()
        # Cancel proactive timer — variable timer takes its place
        self.proactive_service.cancel(channel_id)
        self._variable_timers[channel_id] = asyncio.create_task(
            self._variable_timer_fire(channel_id, channel, seconds)
        )

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

        transcript = self.context_builder.build_context_for_api(
            channel_id=channel_id,
            pending_messages=[],
        )
        proactive_prompt = _load_proactive_prompt()
        timer_note = f"[system: your set_timer for {seconds}s has expired]\n{proactive_prompt}"
        if transcript:
            transcript = f"{transcript}\n{timer_note}"
        else:
            transcript = timer_note

        messages = [{"role": "user", "content": transcript}]
        try:
            async with channel.typing():
                response = await asyncio.to_thread(self.reply_service.generate_reply_with_tools, messages)
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
                self.logger.info(f"⏰ variable_timer_sent channel={channel_id}")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "failed to send variable timer message", exc=exc)

        # Handle any new tool calls (e.g. AI sets another timer)
        self._handle_tool_calls(response, channel_id, channel)

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

        transcript = (
            f"[system: your set_timer for {seconds}s has expired]\n"
            f"你之前答应提醒用户：{reason}\n"
            "请现在提醒用户这件事，不可以沉默。保持你一贯的说话风格和人格。"
        )

        messages = [{"role": "user", "content": transcript}]
        try:
            async with channel.typing():
                response = await asyncio.to_thread(self.reply_service.generate_reply_with_tools, messages)
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
                self.logger.info(f"⏰ alarm_sent channel={channel_id} reason={reason}")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "failed to send alarm message", exc=exc)

        self._handle_tool_calls(response, channel_id, channel)

    def _schedule_proactive(self, channel_id: int, channel: discord.abc.Messageable) -> None:
        """Schedule a proactive idle timer for a channel after bot replies."""

        async def _send_proactive(reply: str, response) -> None:
            try:
                if reply:
                    await self._reply_by_sentence(None, reply, channel=channel)
                    self.history_store.append_entry(
                        channel_id=channel_id,
                        role="assistant",
                        username=self.settings.bot_key,
                        time=self._now_clock(),
                        content=reply,
                    )
                    self.logger.info(f"💬 proactive_sent channel={channel_id}")
                self._handle_tool_calls(response, channel_id, channel)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "failed to send proactive message", exc=exc)

        # Don't schedule proactive if a variable timer is already running
        if channel_id in self._variable_timers:
            return
        self.proactive_service.schedule(channel_id, _send_proactive)

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
        await self.reload_settings_if_needed()
        if message.author.bot:
            return

        text = (message.content or "").strip()
        if not text:
            return
        key = self._typing_key(message.channel.id, message.author.id)
        self._last_message_ts[key] = time.monotonic()

        self._stop_typing_session(message.channel.id, message.author.id, reason="message")
        # Reset proactive idle timer — user is active
        self.proactive_service.cancel(message.channel.id)
        # Cancel variable timer — user is active
        old_vt = self._variable_timers.pop(message.channel.id, None)
        if old_vt is not None and not old_vt.done():
            old_vt.cancel()
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
