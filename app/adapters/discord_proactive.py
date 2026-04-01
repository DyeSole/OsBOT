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
        now_clock: str,
        history_saved: bool = False,
    ) -> None:
        now = time.monotonic()
        pending, opened = self._session_engine.touch_message(
            message=message,
            channel_id=channel_id,
            user_id=user_id,
            user_label=user_label,
            text=text,
            now=now,
            now_clock=now_clock,
            history_saved=history_saved,
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

    # -- common proactive reply -----------------------------------------------

    def _format_prompt(self, target: str, **kwargs: object) -> str:
        template = self.prompt_service.read_prompt(target).strip()
        if not template:
            return ""
        try:
            return template.format(**kwargs).strip()
        except Exception:
            return template

    @staticmethod
    def _bullet_lines(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items if item)

    async def _proactive_reply(
        self,
        channel_id: int,
        channel: discord.abc.Messageable,
        system_note: str,
        *,
        check_silent: bool = True,
        check_new_message: bool = False,
        schedule_proactive: bool = False,
        log_tag: str = "proactive",
    ) -> None:
        """Shared path: build full context (same as user replies), append *system_note*, call LLM."""
        system_entry = {"role": "user", "username": "系统", "time": _now().strftime("%Y-%m-%d %H:%M:%S"), "content": system_note}
        messages, summary = await self._build_messages_for_api(
            channel_id=channel_id,
            pending_messages=[system_entry],
        )

        fire_ts = time.time() if check_new_message else 0.0
        try:
            async with channel.typing():
                response = await asyncio.to_thread(
                    self.reply_service.generate_reply_with_tools, messages, include_tools=True, summary=summary,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", f"{log_tag} api request failed", exc=exc)
            return

        reply = (response.text or "").strip()
        suppress_visible_reply = self._should_suppress_visible_reply(response.tool_calls)
        if suppress_visible_reply:
            reply = ""
        suppressed = False
        if not reply:
            suppressed = True
        elif check_silent and "[SILENT]" in reply:
            suppressed = True
        elif check_new_message and self._channel_has_new_message(channel_id, fire_ts):
            self._log_typing(f"⏰ {log_tag}_suppressed ch={channel_id} (new message arrived)")
            suppressed = True

        if not suppressed:
            try:
                await self._reply_by_sentence(None, reply, channel=channel)
                self._save_bot_reply(channel_id, response.raw_text or reply)
                self._log_typing(f"⏰ {log_tag}_sent ch={channel_id}")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", f"failed to send {log_tag} message", exc=exc)
        elif reply:
            self._log_typing(f"🔇 {log_tag}_silent ch={channel_id}")

        await self._handle_tool_calls(response, channel_id, channel, prior_messages=messages, prior_summary=summary)
        if schedule_proactive:
            self._schedule_proactive(channel_id, channel)

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

        is_typing_nudge = channel_id in self._typing_nudge_channels
        self._typing_nudge_channels.discard(channel_id)
        if is_typing_nudge:
            timer_note = self._format_prompt("typing_nudge_note")
        elif seconds != self.proactive_idle_seconds:
            timer_note = self._format_prompt(
                "timer_expired_note",
                seconds=f"{seconds:g}",
                proactive_prompt=self.prompt_service.read_prompt("proactive").strip(),
            )
        else:
            timer_note = self.prompt_service.read_prompt("proactive").strip()
        pending_reasons = self._pending_alarm_reasons.pop(channel_id, [])
        if pending_reasons:
            timer_note += (
                "\n" + self._format_prompt(
                    "expired_alarm_list_note",
                    alarm_lines=self._bullet_lines(pending_reasons),
                )
            )
        pending_reactions = self._pending_reactions.pop(channel_id, [])
        if pending_reactions:
            timer_note += (
                "\n" + self._format_prompt(
                    "pending_reaction_list_note",
                    reaction_lines=self._bullet_lines(pending_reactions),
                )
            )

        await self._proactive_reply(
            channel_id, channel, timer_note,
            check_silent=True, check_new_message=True, log_tag="timer",
        )

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
        reason: str | None,
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
        reason: str | None,
    ) -> None:
        reason_text = (reason or "").strip() or "闹钟时间到了"
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
            self._quiet_buffered_reasons.setdefault(channel_id, []).append(reason_text)
            self._quiet_channels[channel_id] = channel
            self._schedule_quiet_flush()
            self.logger.info(f"🤫 alarm_buffered_quiet channel={channel_id} reason={reason_text}")
            return

        vt = self._variable_timers.get(channel_id)
        if vt is not None:
            _, deadline = vt
            remaining = deadline - time.monotonic()
            if 0 < remaining < 30:
                self._pending_alarm_reasons.setdefault(channel_id, []).append(reason_text)
                self.logger.info(f"⏰ alarm_buffered channel={channel_id} reason={reason_text} remaining={remaining:.0f}s")
                return

        alarm_note = self._format_prompt(
            "alarm_due_note",
            seconds=f"{seconds:g}",
            reason=reason_text,
        )
        await self._proactive_reply(
            channel_id, channel, alarm_note,
            check_silent=False, log_tag="alarm",
        )

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
            parts: list[str] = []
            parts.append(
                self._format_prompt(
                    "quiet_end_note",
                    morning_prompt=morning_prompt.strip(),
                )
            )
            reasons = buffered.get(channel_id, [])
            if reasons:
                parts.append(
                    self._format_prompt(
                        "expired_alarm_list_note",
                        alarm_lines=self._bullet_lines(reasons),
                    )
                )
            parts.append(proactive_prompt.strip())

            await self._proactive_reply(
                channel_id, channel, "\n".join(part for part in parts if part),
                check_silent=False, log_tag="morning",
            )

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
        self.logger.info(f"👁️ watch_idle_fire user={user_id} ch={channel.id}")
        minutes = int(self.watch_online_idle_seconds // 60) or 1
        raw_prompt = self.prompt_service.read_prompt("watch_online").strip()
        if not raw_prompt:
            return
        timer_note = raw_prompt.replace("{minutes}", str(minutes))

        await self._proactive_reply(
            channel.id, channel, timer_note,
            check_silent=True, schedule_proactive=True, log_tag="watch",
        )

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
        jealousy_ids = set(self.settings.jealousy_channel_ids or [])
        best_ch: discord.abc.Messageable | None = None
        best_ts: float = 0.0
        for (ch_id, uid), ts in self._last_message_ts.items():
            if uid == user_id and ts > best_ts and str(ch_id) not in jealousy_ids:
                ch = self.client.get_channel(ch_id)
                if ch is not None and getattr(ch, "guild", None) == guild:
                    best_ts = ts
                    best_ch = ch
        if best_ch is None:
            self._log_typing(f"💚 find_channel_fail user={user_id} no_candidate jealousy_ids={jealousy_ids}")
        return best_ch

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
            self._log_typing(f"💚 jealousy_skip reason=no_guild ch={channel.id} user={uid_str}")
            return
        bot_channel = self._find_channel_for_user(user.id, guild)
        if bot_channel is None:
            self._log_typing(f"💚 jealousy_skip reason=no_bot_channel user={uid_str}")
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
        target = "jealousy_quiet" if is_quiet else "jealousy"
        raw_prompt = self.prompt_service.read_prompt(target).strip()
        if not raw_prompt:
            return
        jealousy_note = raw_prompt.replace("{count}", str(count))

        await self._proactive_reply(
            channel_id, channel, jealousy_note,
            check_silent=True, log_tag="jealousy",
        )
