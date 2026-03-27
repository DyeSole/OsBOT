"""Proactive mixin: timers, alarms, quiet hours, typing sessions, watch online, jealousy."""
from __future__ import annotations

import asyncio
import json as _json
import time
from dataclasses import dataclass
from datetime import timedelta, time as dt_time

from app.core.clock import now as _now

import discord


@dataclass
class TypingSession:
    started_at: float
    last_seen_at: float
    channel_label: str
    user_label: str


class ProactiveMixin:
    """Methods for proactive messaging, timers, alarms, quiet hours, presence, and jealousy."""

    # -- typing sessions ------------------------------------------------------

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

    # -- variable timer / proactive -------------------------------------------

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
                self._save_bot_reply(channel_id, reply)
                self._log_typing(f"⏰ timer_sent ch={channel_id} reply={reply}")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "failed to send variable timer message", exc=exc)
        else:
            self._log_typing(f"🔇 time_fire ch={channel_id}")

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

    # -- alarms ---------------------------------------------------------------

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
                self._save_bot_reply(channel_id, reply)
                self._log_typing(f"⏰ alarm_sent ch={channel_id} reason={reason} reply={reply}")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "failed to send alarm message", exc=exc)

        await self._handle_tool_calls(response, channel_id, channel, prior_messages=messages)

    # -- quiet hours ----------------------------------------------------------

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
                    self._save_bot_reply(channel_id, reply)
                    self._log_typing(f"🌅 morning ch={channel_id} alarms={len(reasons)}")
                except Exception as exc:  # noqa: BLE001
                    self.logger.error("UNKNOWN", "failed to send morning message", exc=exc)

            await self._handle_tool_calls(response, channel_id, channel, prior_messages=messages)

    # -- watch online / presence ----------------------------------------------

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
                self._save_bot_reply(channel_id, reply)
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

    # -- jealousy -------------------------------------------------------------

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
                self._save_bot_reply(channel_id, reply)
                self.logger.info(
                    f"🏷️ jealousy_sent user={user_id} ch={channel_id} msg_count={count}"
                    f" | prompt={jealousy_note}"
                    f" | reply={reply}"
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "failed to send jealousy message", exc=exc)
        await self._handle_tool_calls(response, channel_id, channel, prior_messages=messages)
