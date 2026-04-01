from __future__ import annotations

import asyncio
import time

from app.core.clock import now_clock as _now_clock_util

import discord
from discord import app_commands

from app.config.settings import BASE_DIR, Settings, env_last_modified, load_settings
from app.core.logging import BotLogger
from app.core.session_engine import SessionEngine
from app.infra.storage import ChatHistoryStore, CompressionStore
from app.services.compression_service import CompressionService
from app.services.context_builder import ContextBuilder
from app.services.prompt_service import PromptService
from app.services.reply_service import ReplyService
from app.adapters.discord_ui import ToolboxView
from app.adapters.discord_dispatch import DispatchMixin
from app.adapters.discord_proactive import ProactiveMixin
from app.infra.hf_image_client import HFImageClient
from app.infra.pixai_client import PixAIClient


class DiscordBot(DispatchMixin, ProactiveMixin):
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
        self.pixai_client = PixAIClient(settings.pixai_tokens)
        self.hf_image_client = HFImageClient(
            settings.hf_image_api_key,
            settings.hf_image_model,
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
        self._typing_sessions: dict[tuple[int, int], object] = {}
        self._session_engine = SessionEngine()
        self._last_message_ts: dict[tuple[int, int], float] = {}
        self._last_msg_ts_path = BASE_DIR / "data" / "last_message_ts.json"
        self._load_last_message_ts()
        self._typing_watchdog_task: asyncio.Task | None = None
        self._variable_timers: dict[int, tuple[asyncio.Task, float]] = {}
        self._alarms: dict[int, list[asyncio.Task]] = {}
        self._pending_alarm_reasons: dict[int, list[str]] = {}
        self._pending_reactions: dict[int, list[str]] = {}
        self._typing_nudge_channels: set[int] = set()
        self._auto_effective_mode: str = "chat"
        self._quiet_buffered_reasons: dict[int, list[str]] = {}
        self._quiet_channels: dict[int, discord.abc.Messageable] = {}
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

        self._watch_previous_status: dict[int, str] = {}
        self._watch_online_timers: dict[int, asyncio.Task] = {}
        self._jealousy_counts: dict[int, int] = {}
        self._jealousy_timers: dict[int, asyncio.Task] = {}

        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self.client.event(self.on_message_edit)
        self.client.event(self.on_typing)
        self.client.event(self.on_raw_reaction_add)
        self.client.event(self.on_guild_channel_delete)
        self.client.event(self.on_presence_update)
        self._register_app_commands()

    # -- app commands ---------------------------------------------------------

    def _register_app_commands(self) -> None:
        @self.tree.command(name="工具箱", description="打开工具箱")
        async def toolbox(interaction: discord.Interaction) -> None:
            allowed_ids = self.settings.watch_user_ids
            if allowed_ids and str(interaction.user.id) not in allowed_ids:
                embed = discord.Embed(
                    title="⛔ 没有权限",
                    description="你没有权限使用工具箱。",
                    color=discord.Color.red(),
                )
                await interaction.response.send_message(
                    embed=embed, ephemeral=True,
                )
                return
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

    # -- settings -------------------------------------------------------------

    def apply_settings(self, settings: Settings) -> None:
        old_token = self.settings.discord_bot_token
        self.settings = settings
        self.reply_service.apply_settings(settings)
        if settings.split_mode != "auto":
            self._auto_effective_mode = "chat"
        self.reply_service.effective_mode = self.effective_split_mode
        self.pixai_client.set_tokens(settings.pixai_tokens)
        self.hf_image_client.apply_settings(
            settings.hf_image_api_key,
            settings.hf_image_model,
        )
        self.compression_service.apply_settings(settings)
        self.logger.bot_key = self._effective_bot_key()
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

    # -- context building -----------------------------------------------------

    async def _build_context_for_api(
        self,
        *,
        channel_id: int,
        pending_messages: list[dict[str, str]],
    ) -> str:
        messages, summary = self.context_builder.build_messages_for_api(
            channel_id=channel_id,
            pending_messages=pending_messages,
        )
        input_tokens = self.reply_service.count_input_tokens(messages, summary=summary)
        if self.settings.app_mode == "debug":
            self.logger.info(
                f"🧮 transcript_tokens ch={channel_id} input_tokens={input_tokens} "
                f"limit={self.settings.transcript_max_tokens}"
            )
        transcript = self.context_builder.build_context_for_api(
            channel_id=channel_id,
            pending_messages=pending_messages,
        )
        if input_tokens <= self.settings.transcript_max_tokens:
            return transcript

        self.logger.info(
            f"🗜️ transcript_over_limit ch={channel_id} input_tokens={input_tokens} "
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

    async def _build_messages_for_api(
        self,
        *,
        channel_id: int,
        pending_messages: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], str]:
        """Return (messages, summary) in alternating format.

        Handles auto-compression when token limit is exceeded.
        """
        messages, summary = self.context_builder.build_messages_for_api(
            channel_id=channel_id,
            pending_messages=pending_messages,
        )
        input_tokens = self.reply_service.count_input_tokens(messages, summary=summary)
        if self.settings.app_mode == "debug":
            self.logger.info(
                f"🧮 transcript_tokens ch={channel_id} input_tokens={input_tokens} "
                f"limit={self.settings.transcript_max_tokens}"
            )
        if input_tokens > self.settings.transcript_max_tokens:
            self.logger.info(
                f"🗜️ transcript_over_limit ch={channel_id} input_tokens={input_tokens} "
                f"limit={self.settings.transcript_max_tokens} -> compress"
            )
            try:
                await asyncio.to_thread(
                    self.compression_service.compress_history,
                    channel_id=channel_id,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "auto compression failed", exc=exc)

        return self.context_builder.build_messages_for_api(
            channel_id=channel_id,
            pending_messages=pending_messages,
        )

    # -- utilities ------------------------------------------------------------

    def _typing_key(self, channel_id: int, user_id: int) -> tuple[int, int]:
        return (channel_id, user_id)

    @property
    def effective_split_mode(self) -> str:
        mode = self.settings.split_mode
        if mode == "auto":
            return self._auto_effective_mode
        return mode

    def _channel_has_new_message(self, channel_id: int, since: float) -> bool:
        for (ch_id, _uid), ts in self._last_message_ts.items():
            if ch_id == channel_id and ts > since:
                return True
        return False

    @staticmethod
    def _now_clock() -> str:
        return _now_clock_util()

    def _save_entry(
        self, channel_id: int, role: str, username: str, content: str, *, at: str = "",
    ) -> None:
        self.history_store.append_entry(
            channel_id=channel_id,
            role=role,
            username=username,
            time=at or self._now_clock(),
            content=content,
        )

    def _save_bot_reply(self, channel_id: int, content: str) -> None:
        self._save_entry(channel_id, "assistant", self._effective_bot_key(), content)

    def _effective_bot_key(self) -> str:
        configured = (self.settings.bot_key or "").strip()
        if configured:
            return configured
        user = self.client.user
        if user is not None:
            display_name = getattr(user, "display_name", None)
            if isinstance(display_name, str) and display_name.strip():
                return display_name.strip()
            name = getattr(user, "name", None)
            if isinstance(name, str) and name.strip():
                return name.strip()
        return "Bot"

    @staticmethod
    def _extract_saved_image_descs(content: str) -> list[str]:
        return [
            line.strip()
            for line in (content or "").splitlines()
            if line.strip().startswith("[图片:")
        ]

    async def _build_message_text(
        self,
        message: discord.Message,
        *,
        image_descs: list[str] | None = None,
    ) -> str:
        text = (message.content or "").strip()
        if not text and message.stickers:
            names = "、".join(s.name for s in message.stickers)
            text = f"[贴纸: {names}]"

        if image_descs:
            text = (text + "\n" if text else "") + "\n".join(image_descs)

        if not text:
            return ""

        ref = message.reference
        if ref and ref.message_id:
            try:
                quoted = ref.resolved or await message.channel.fetch_message(ref.message_id)
                if quoted and getattr(quoted, "content", None):
                    quote_author = self._user_label(quoted.author)
                    text = f"[引用 {quote_author} 的消息: {quoted.content}]\n{text}"
            except Exception:  # noqa: BLE001
                pass

        return text

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

    # -- env watcher ----------------------------------------------------------

    async def _watch_env_changes(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.reload_settings_if_needed()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("CONFIG", "failed to hot reload .env", exc=exc)

    # -- discord events -------------------------------------------------------

    async def on_ready(self) -> None:
        self.logger.bot_key = self._effective_bot_key()
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

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        cid = channel.id
        self.logger.info(f"🗑️ channel_deleted id={cid} name={getattr(channel, 'name', '?')}")
        h = self.history_store.delete_channel(cid)
        m = self.compression_store.delete_channel(cid)
        if h or m:
            self.logger.info(f"🧹 cleaned channel_id={cid} history={h} memory={m}")

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

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.author.bot:
            return
        if before.content == after.content:
            return
        channel_id = after.channel.id
        entries = self.history_store.load_all_entries(channel_id=channel_id)
        if not entries:
            return
        if entries[-1].get("role") != "assistant":
            return
        last_user_entry = next((e for e in reversed(entries) if e["role"] == "user"), None)
        if last_user_entry is None:
            return

        async for msg in after.channel.history(after=after, limit=50):
            if not msg.author.bot:
                return

        new_text = await self._build_message_text(
            after,
            image_descs=self._extract_saved_image_descs(last_user_entry["content"]),
        )
        if not new_text:
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
        self._delete_latest_bot_turn_db(channel_id)
        await self._regenerate_reply(channel_id, after.channel)

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
            self._delete_latest_bot_turn_db(channel_id)
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

    async def on_message(self, message: discord.Message) -> None:
        await self.reload_settings_if_needed()
        if message.author.bot:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        # DM 只允许 watch_user_ids 里的用户
        if isinstance(message.channel, discord.DMChannel):
            allowed_ids = self.settings.watch_user_ids
            if allowed_ids and str(message.author.id) not in allowed_ids:
                return

        now_clock = self._now_clock()
        image_descs, history_saved = await self._describe_attachments(message)
        text = await self._build_message_text(
            message,
            image_descs=[] if history_saved else image_descs,
        )
        if not text:
            return
        key = self._typing_key(message.channel.id, message.author.id)
        self._last_message_ts[key] = time.time()
        self._save_last_message_ts()

        self.logger.info(f"📩 msg_received user={self._user_label(message.author)} ch={message.channel.id} text={text[:80]}")
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
                now_clock=now_clock,
                history_saved=history_saved,
            )
        else:
            asyncio.create_task(
                self._reply_immediate(message, text, history_saved=history_saved)
            )

    # -- lifecycle ------------------------------------------------------------

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
