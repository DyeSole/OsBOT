"""Dispatch mixin: message sending, streaming, tool handling, search, and regeneration."""
from __future__ import annotations

import asyncio
import queue as _queue
import re
import time
from typing import TYPE_CHECKING

import discord
from discord import AllowedMentions

if TYPE_CHECKING:
    from app.infra.llm_client import LLMResponse

_TAG_RE = re.compile(r"\[(?:TIMER|REACTION|IMAGE|VOICE|SEARCH):\s*[^\]]+\]")
_SWITCH_MODE_RE = re.compile(r"\[SWITCH_MODE:\s*(chat|novel)\]")
_TIME_TAG_RE = re.compile(r"\[(?:[01]?\d|2[0-3]):[0-5]\d\]\s*")


class DispatchMixin:
    """Methods for dispatching API calls, streaming replies, and handling tools."""

    # -- text utilities -------------------------------------------------------

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

    @staticmethod
    def _chunk_text(text: str, *, limit: int = 2000) -> list[str]:
        stripped = (text or "").strip()
        if not stripped:
            return []
        return [stripped[i:i + limit] for i in range(0, len(stripped), limit)]

    @staticmethod
    def _should_suppress_visible_reply(tool_calls: list[object]) -> bool:
        tool_names = {
            getattr(tc, "name", "")
            for tc in tool_calls
            if getattr(tc, "name", "")
        }
        silent_tools = {"add_reaction", "generate_image", "send_voice"}
        blocking_tools = {"set_timer", "web_search"}
        return bool(tool_names & silent_tools) and not bool(tool_names & blocking_tools)

    # -- sending helpers ------------------------------------------------------

    async def _reply_by_sentence(
        self,
        anchor_message: discord.Message | None,
        reply: str,
        *,
        channel: discord.abc.Messageable | None = None,
    ) -> None:
        do_split = self.effective_split_mode == "chat"
        sentences = self._split_sentences(reply, split=do_split)
        if not sentences:
            return
        target_channel = channel or (anchor_message.channel if anchor_message else None)
        if target_channel is None:
            return
        for idx, sentence in enumerate(sentences):
            await target_channel.send(
                sentence,
                allowed_mentions=AllowedMentions.none(),
            )
            if idx < len(sentences) - 1:
                await asyncio.sleep(0.8)

    @staticmethod
    def _filter_tags(raw: str, tag_hold: str) -> tuple[str, str, str | None]:
        """Filter [TAG: ...] markers from streaming text.

        Returns (safe_text_to_send, remaining_hold_buffer, detected_mode).
        detected_mode is "chat"/"novel" if a SWITCH_MODE tag was found, else None.
        """
        combined = tag_hold + raw
        safe = []
        hold = ""
        detected_mode: str | None = None
        i = 0
        while i < len(combined):
            ch = combined[i]
            if hold:
                hold += ch
                if ch == "]":
                    # check SWITCH_MODE first (swallow + detect mode)
                    m = _SWITCH_MODE_RE.fullmatch(hold)
                    if m:
                        detected_mode = m.group(1)
                        hold = ""  # swallow the tag
                    elif _TIME_TAG_RE.fullmatch(hold):
                        hold = ""  # swallow llm-added [HH:MM] markers
                    elif _TAG_RE.fullmatch(hold):
                        hold = ""  # swallow the tag
                    else:
                        safe.append(hold)
                        hold = ""
                elif len(hold) > 500:
                    # safety: not a tag, release
                    safe.append(hold)
                    hold = ""
            elif ch == "[":
                hold = ch
            else:
                safe.append(ch)
            i += 1
        return "".join(safe), hold, detected_mode

    async def _stream_and_send(
        self,
        anchor_message: discord.Message | None,
        channel: discord.abc.Messageable,
        messages: list[dict[str, str]],
        *,
        include_tools: bool = False,
        summary: str = "",
    ) -> tuple[LLMResponse, list[discord.Message]]:
        from app.infra.llm_client import LLMResponse

        q: _queue.Queue[tuple[str, object]] = _queue.Queue()

        async def _sync_novel_messages(
            full_text: str,
            novel_msgs: list[discord.Message],
            sent_msgs: list[discord.Message],
        ) -> None:
            chunks = self._chunk_text(full_text)
            if not chunks:
                return
            for idx, chunk in enumerate(chunks):
                if idx < len(novel_msgs):
                    await novel_msgs[idx].edit(content=chunk)
                else:
                    msg = await channel.send(
                        chunk,
                        allowed_mentions=AllowedMentions.none(),
                    )
                    novel_msgs.append(msg)
                    sent_msgs.append(msg)

        def _produce() -> None:
            try:
                resp = self.reply_service.stream_reply_with_tools(
                    messages, lambda chunk: q.put(("text", chunk)),
                    include_tools=include_tools,
                    summary=summary,
                )
                q.put(("done", resp))
            except Exception as exc:  # noqa: BLE001
                q.put(("error", exc))

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _produce)

        buffer = ""
        tag_hold = ""
        is_first = True
        sent_msgs: list[discord.Message] = []
        is_novel = self.effective_split_mode == "novel"
        novel_msgs: list[discord.Message] = []
        novel_full = ""
        novel_last_edit = 0.0

        while True:
            try:
                kind, value = q.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.05)
                continue

            if kind == "text":
                filtered, tag_hold, detected_mode = self._filter_tags(value, tag_hold)
                if detected_mode:
                    is_novel = detected_mode == "novel"
                    if is_novel and not novel_msgs:
                        # switching to novel mid-stream: move buffered text into novel_full
                        novel_full = buffer
                if not filtered:
                    continue
                buffer += filtered
                if is_novel:
                    novel_full += filtered
                    now = asyncio.get_event_loop().time()
                    if now - novel_last_edit >= 1.0:
                        display = novel_full.strip()
                        if display:
                            try:
                                await _sync_novel_messages(display, novel_msgs, sent_msgs)
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
                            msg = await channel.send(
                                s, allowed_mentions=AllowedMentions.none(),
                            )
                            sent_msgs.append(msg)
                            is_first = False
                        buffer = parts[-1]
            elif kind == "done":
                # flush any remaining tag_hold (incomplete tag = not a tag)
                if tag_hold:
                    buffer += tag_hold
                    if is_novel:
                        novel_full += tag_hold
                    tag_hold = ""
                if is_novel:
                    display = novel_full.strip()
                    if display:
                        try:
                            await _sync_novel_messages(display, novel_msgs, sent_msgs)
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    if buffer.strip():
                        if not is_first and self.settings.chat_reply_delay_seconds > 0:
                            async with channel.typing():
                                await asyncio.sleep(self.settings.chat_reply_delay_seconds)
                        msg = await channel.send(
                            buffer.strip(), allowed_mentions=AllowedMentions.none(),
                        )
                        sent_msgs.append(msg)
                return value, sent_msgs  # type: ignore[return-value]
            elif kind == "error":
                raise value  # type: ignore[misc]

    # -- dispatch -------------------------------------------------------------

    async def _send_and_finalize(
        self,
        channel_id: int,
        channel: discord.abc.Messageable,
        messages: list[dict[str, str]],
        anchor_message: discord.Message | None,
        *,
        summary: str = "",
    ) -> None:
        """Shared tail: stream reply, save, handle tools, schedule proactive."""
        try:
            await channel.typing()
            response, sent_msgs = await self._stream_and_send(
                anchor_message, channel, messages, summary=summary,
            )
            reply = (response.text or "").strip()
            suppress_visible_reply = self._should_suppress_visible_reply(response.tool_calls)
            if suppress_visible_reply and sent_msgs:
                await self._delete_messages(sent_msgs)
                sent_msgs = []
                reply = ""
            has_search = any(tc.name == "web_search" for tc in response.tool_calls)
            if reply and not has_search:
                self._save_bot_reply(channel_id, reply)
            edit_msg = sent_msgs[-1] if sent_msgs and has_search else None
            await self._handle_tool_calls(
                response,
                channel_id,
                channel,
                prior_messages=messages,
                prior_summary=summary,
                edit_msg=edit_msg,
                had_reply=bool(reply),
                source_message=anchor_message,
            )
            self._schedule_proactive(channel_id, channel)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "failed to send reply", exc=exc)
            self._save_bot_reply(channel_id, "[系统: API请求失败]")
            try:
                await channel.send("我刚刚有点卡住了，等我一下再试试。")
            except Exception:
                pass

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

        self._save_entry(channel_id, "user", pending.user_label, merged_text, at=pending.first_time)
        messages, summary = await self._build_messages_for_api(
            channel_id=channel_id,
            pending_messages=[],
        )
        if not messages:
            self.logger.error("LOGIC", "empty messages, skip api request")
            return
        merged_one_line = merged_text.replace("\n", "\\n")
        now_send = time.monotonic()
        wait_from_last_msg = now_send - pending.last_message_at
        self.logger.info(
            f"✅ api_request_sent wait_from_last_msg={wait_from_last_msg:.2f}s includes={merged_one_line}"
        )
        self._log_typing(
            f"🚀 api_sent user={pending.user_label} chunks={len(pending.chunks)} merged_len={len(merged_text)}"
        )

        await self._send_and_finalize(channel_id, pending.channel, messages, pending.anchor_message, summary=summary)

    async def _reply_immediate(self, message: discord.Message, text: str) -> None:
        channel_id = message.channel.id
        user_label = self._user_label(message.author)
        now_clock = self._now_clock()

        self._save_entry(channel_id, "user", user_label, text, at=now_clock)
        messages, summary = await self._build_messages_for_api(
            channel_id=channel_id,
            pending_messages=[],
        )
        if not messages:
            self.logger.error("LOGIC", "empty messages, skip api request")
            return
        text_one_line = text.replace("\n", "\\n")
        self.logger.info(f"✅ api_request_sent (immediate) includes={text_one_line}")

        await self._send_and_finalize(channel_id, message.channel, messages, message, summary=summary)

    # -- tool calls -----------------------------------------------------------

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
        prior_summary: str = "",
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
                    self._save_bot_reply(channel_id, confirm)
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
            if tc.name == "send_voice":
                voice_text = str(tc.input.get("text", "")).strip()
                if voice_text:
                    await self._send_voice(voice_text, channel_id, channel, source_message)
            if tc.name == "generate_image":
                prompt = str(tc.input.get("prompt", "")).strip()
                if prompt:
                    await self._send_generated_image(prompt, channel_id, channel, source_message)
            if tc.name == "switch_mode":
                new_mode = tc.input.get("mode", "")
                if new_mode in ("chat", "novel") and new_mode != self._auto_effective_mode:
                    self._auto_effective_mode = new_mode
                    self.reply_service.effective_mode = new_mode
                    label = "小说模式" if new_mode == "novel" else "聊天模式"
                    self.logger.info(f"🔄 switch_mode → {label}")
            if tc.name == "web_search":
                query = tc.input.get("query", "")
                if query:
                    if search_depth >= 3:
                        self.logger.info(f"🔍 search_depth_limit query={query} depth={search_depth}")
                    else:
                        if search_depth == 0:
                            await self._add_reaction(source_message, "🔍")
                        await self._execute_search(
                            query,
                            channel_id,
                            channel,
                            prior_messages,
                            summary=prior_summary,
                            search_depth=search_depth,
                            edit_msg=edit_msg,
                        )
                        if search_depth == 0:
                            await self._remove_reaction(source_message, "🔍")

    # -- shared error feedback ------------------------------------------------

    async def _send_error_feedback(
        self,
        channel_id: int,
        channel: discord.abc.Messageable,
        title: str,
        detail: str,
        history_text: str,
    ) -> None:
        """Send a red embed error + write to chat history. Shared by voice & image."""
        self._save_bot_reply(channel_id, history_text)
        try:
            embed = discord.Embed(title=title, description=detail, color=discord.Color.red())
            await channel.send(embed=embed)
        except Exception:  # noqa: BLE001
            pass

    # -- reaction helpers -----------------------------------------------------

    @staticmethod
    async def _add_reaction(msg: discord.Message | None, emoji: str) -> None:
        if msg is None:
            return
        try:
            await msg.add_reaction(emoji)
        except Exception:  # noqa: BLE001
            pass

    async def _remove_reaction(self, msg: discord.Message | None, emoji: str) -> None:
        if msg is None:
            return
        try:
            me = msg.guild.me if msg.guild else self.client.user  # type: ignore[union-attr]
            await msg.remove_reaction(emoji, me)
        except Exception:  # noqa: BLE001
            pass

    # -- voice ----------------------------------------------------------------

    async def _send_voice(
        self,
        text: str,
        channel_id: int,
        channel: discord.abc.Messageable,
        source_message: discord.Message | None = None,
    ) -> None:
        from app.infra.tts_client import synthesize

        settings = self.settings
        if not settings.tts_api_key or not settings.tts_voice_id:
            self.logger.info("🔇 tts skipped: no api_key or voice_id")
            return

        await self._add_reaction(source_message, "🎤")
        try:
            audio_bytes = await asyncio.to_thread(
                synthesize,
                text,
                api_key=settings.tts_api_key,
                voice_id=settings.tts_voice_id,
                speed=settings.tts_speed,
                pitch=settings.tts_pitch,
                emotion=settings.tts_emotion,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "tts synthesize failed", exc=exc)
            await self._remove_reaction(source_message, "🎤")
            await self._send_error_feedback(
                channel_id, channel, "⛔ 语音合成失败", str(exc), "[系统: 语音合成失败]",
            )
            return

        if not audio_bytes:
            self.logger.info("🔇 tts returned empty audio")
            await self._remove_reaction(source_message, "🎤")
            await self._send_error_feedback(
                channel_id, channel, "⛔ 语音合成失败", "TTS 返回空音频", "[系统: 语音合成失败]",
            )
            return

        import io
        file = discord.File(io.BytesIO(audio_bytes), filename="voice.mp3")
        try:
            await channel.send(file=file)
            self._save_bot_reply(channel_id, f"[语音: {text}]")
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "failed to send voice message", exc=exc)
        await self._remove_reaction(source_message, "🎤")

    # -- image generation -----------------------------------------------------

    async def _send_generated_image(
        self,
        prompt: str,
        channel_id: int,
        channel: discord.abc.Messageable,
        source_message: discord.Message | None = None,
    ) -> None:
        if not self.hf_image_client.available and not self.pixai_client.available:
            self.logger.info("🎨 image generation skipped: no provider configured")
            return

        await self._add_reaction(source_message, "🎨")
        image_bytes: bytes | None = None
        send_error: Exception | None = None

        if self.hf_image_client.available:
            try:
                image_bytes = await asyncio.to_thread(self.hf_image_client.generate_image, prompt)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "huggingface image generate failed", exc=exc)
                send_error = exc

        if image_bytes is None and self.pixai_client.available:
            try:
                url = await asyncio.to_thread(self.pixai_client.generate_image, prompt)
                import requests as _req
                resp = await asyncio.to_thread(lambda: _req.get(url, timeout=60))
                resp.raise_for_status()
                if len(resp.content) < 1000:
                    raise RuntimeError(f"image too small ({len(resp.content)} bytes), likely not a real image")
                image_bytes = resp.content
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "pixai generate failed", exc=exc)
                send_error = exc

        if image_bytes is None:
            await self._remove_reaction(source_message, "🎨")
            await self._send_error_feedback(
                channel_id, channel, "⛔ 图片生成失败", str(send_error or "no image provider available"), "[系统: 图片生成失败]",
            )
            return

        try:
            import io
            file = discord.File(io.BytesIO(image_bytes), filename="image.png")
            await channel.send(file=file)
            self._save_bot_reply(channel_id, f"[图片: {prompt}]")
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "failed to send generated image", exc=exc)

        await self._remove_reaction(source_message, "🎨")

    # -- search ---------------------------------------------------------------

    async def _execute_search(
        self,
        query: str,
        channel_id: int,
        channel: discord.abc.Messageable,
        prior_messages: list[dict[str, str]] | None = None,
        *,
        summary: str = "",
        search_depth: int = 0,
        edit_msg: discord.Message | None = None,
    ) -> None:
        from app.infra.search_client import web_search
        from app.services.reply_service import load_system_prompt

        self.logger.info(f"🔍 web_search query={query} depth={search_depth}")
        recent_entries = self.history_store.load_all_entries(channel_id=channel_id)
        context_hint = self.history_store.render_entries(recent_entries[-10:]) if recent_entries else ""
        soul = load_system_prompt(effective_mode=self.effective_split_mode)
        try:
            results = await asyncio.to_thread(
                web_search,
                query,
                base_url=self.settings.search_base_url or self.settings.base_url,
                api_key=self.settings.search_api_key or self.settings.api_key,
                model=self.settings.search_model or "grok-4.1-fast",
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
            if prior_messages is not None:
                messages = list(prior_messages)
            else:
                messages, summary = await self._build_messages_for_api(
                    channel_id=channel_id,
                    pending_messages=[],
                )
            messages.append({"role": "user", "content": search_block})
        else:
            messages = list(prior_messages) if prior_messages else []
            messages.append({"role": "user", "content": search_block})

        next_depth = search_depth + 1
        try:
            async with channel.typing():
                search_response = await asyncio.to_thread(
                    self.reply_service.generate_reply_with_tools,
                    messages,
                    include_tools=True,
                    summary=summary,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "search follow-up api request failed", exc=exc)
            await self._send_error_feedback(
                channel_id, channel, "⛔ 搜索失败", str(exc), "[系统: 搜索失败]",
            )
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
                next_search.input["query"],
                channel_id,
                channel,
                messages,
                summary=summary,
                search_depth=next_depth,
                edit_msg=edit_msg,
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
                self._save_bot_reply(channel_id, reply)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("UNKNOWN", "failed to send search reply", exc=exc)

        await self._handle_tool_calls(
            search_response,
            channel_id,
            channel,
            prior_messages=messages,
            prior_summary=summary,
            search_depth=next_depth,
            edit_msg=edit_msg,
        )

    # -- message editing & regeneration ---------------------------------------

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
        messages, summary = await self._build_messages_for_api(
            channel_id=channel_id,
            pending_messages=[],
        )
        if not messages:
            return
        try:
            async with channel.typing():
                response, sent_msgs = await self._stream_and_send(
                    None, channel, messages, summary=summary,
                )
            reply = (response.text or "").strip()
            suppress_visible_reply = self._should_suppress_visible_reply(response.tool_calls)
            if suppress_visible_reply and sent_msgs:
                await self._delete_messages(sent_msgs)
                sent_msgs = []
                reply = ""
            has_search = any(tc.name == "web_search" for tc in response.tool_calls)
            if reply and not has_search:
                self._save_bot_reply(channel_id, reply)
            edit_msg = sent_msgs[-1] if sent_msgs and has_search else None
            await self._handle_tool_calls(
                response,
                channel_id,
                channel,
                prior_messages=messages,
                prior_summary=summary,
                edit_msg=edit_msg,
            )
            self._schedule_proactive(channel_id, channel)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("UNKNOWN", "regenerate reply failed", exc=exc)

    # -- image processing -----------------------------------------------------

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

        vision_prompt = self.prompt_service.read_prompt("vision").strip()
        channel_id = message.channel.id
        recent = self.history_store.load_all_entries(channel_id=channel_id)[-self.settings.context_entries:]
        context = self.history_store.render_entries(recent) if recent else ""
        await self._add_reaction(message, "👁️")
        status_msg = await message.channel.send("正在识图...")
        descriptions: list[str] = []

        for att in message.attachments:
            ct = att.content_type or ""
            media_type = ct.split(";")[0].strip().lower()
            if media_type not in IMAGE_TYPES:
                continue
            try:
                image_bytes = await att.read()
                desc = await asyncio.to_thread(
                    vision.describe_image, image_bytes, media_type, system_prompt=vision_prompt, context=context,
                )
                if desc:
                    descriptions.append(f"[图片: {desc}]")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("VISION", f"failed to describe attachment {att.filename}", exc=exc)
                descriptions.append(f"[图片: {att.filename} 识别失败]")

        try:
            await status_msg.delete()
        except Exception:  # noqa: BLE001
            pass
        await self._remove_reaction(message, "👁️")

        return descriptions
