"""Discord UI components – modals, views, and buttons for the toolbox."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from app.config.settings import read_env_values, update_env_values

if TYPE_CHECKING:
    from app.adapters.discord_bot import DiscordBot


class ApiConfigModal(discord.ui.Modal, title="聊天 API 配置"):
    def __init__(self, bot: DiscordBot):
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
            content="API 配置\n\n聊天 API 配置已写入 .env，文件监听会自动生效。",
            view=ApiToolboxView(self.bot),
        )


class VisionConfigModal(discord.ui.Modal, title="识图 API 配置"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        env_values = read_env_values()
        current = bot.settings
        self.vision_base_url = discord.ui.TextInput(
            label="VISION_BASE_URL",
            default=env_values.get("VISION_BASE_URL", current.vision_base_url),
            required=False,
            max_length=400,
            placeholder="留空则使用聊天 API 的 BASE_URL",
        )
        self.vision_api_key = discord.ui.TextInput(
            label="VISION_API_KEY",
            default=env_values.get("VISION_API_KEY", current.vision_api_key),
            required=False,
            max_length=400,
            placeholder="留空则使用聊天 API 的 API_KEY",
        )
        self.vision_model = discord.ui.TextInput(
            label="VISION_MODEL",
            default=env_values.get("VISION_MODEL", current.vision_model),
            required=False,
            max_length=120,
            placeholder="留空则使用聊天 API 的 MODEL",
        )
        self.vision_prompt = discord.ui.TextInput(
            label="VISION_PROMPT（识图系统提示词）",
            style=discord.TextStyle.paragraph,
            default=env_values.get("VISION_PROMPT", current.vision_prompt),
            required=False,
            max_length=800,
            placeholder="留空则不传 system prompt",
        )
        self.add_item(self.vision_base_url)
        self.add_item(self.vision_api_key)
        self.add_item(self.vision_model)
        self.add_item(self.vision_prompt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        update_env_values(
            {
                "VISION_BASE_URL": self.vision_base_url.value.strip(),
                "VISION_API_KEY": self.vision_api_key.value.strip(),
                "VISION_MODEL": self.vision_model.value.strip(),
                "VISION_PROMPT": self.vision_prompt.value.strip(),
            }
        )
        await interaction.response.edit_message(
            content="API 配置\n\n识图 API 配置已写入 .env，文件监听会自动生效。",
            view=ApiToolboxView(self.bot),
        )


class SearchConfigModal(discord.ui.Modal, title="搜索 API 配置"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        env_values = read_env_values()
        current = bot.settings
        self.search_base_url = discord.ui.TextInput(
            label="SEARCH_BASE_URL",
            default=env_values.get("SEARCH_BASE_URL", current.search_base_url),
            required=False,
            max_length=400,
            placeholder="留空则使用 DuckDuckGo",
        )
        self.search_api_key = discord.ui.TextInput(
            label="SEARCH_API_KEY",
            default=env_values.get("SEARCH_API_KEY", current.search_api_key),
            required=False,
            max_length=400,
            placeholder="留空则使用 DuckDuckGo",
        )
        self.search_model = discord.ui.TextInput(
            label="SEARCH_MODEL",
            default=env_values.get("SEARCH_MODEL", current.search_model),
            required=False,
            max_length=120,
            placeholder="留空默认 grok-3-mini-fast",
        )
        self.add_item(self.search_base_url)
        self.add_item(self.search_api_key)
        self.add_item(self.search_model)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        update_env_values(
            {
                "SEARCH_BASE_URL": self.search_base_url.value.strip(),
                "SEARCH_API_KEY": self.search_api_key.value.strip(),
                "SEARCH_MODEL": self.search_model.value.strip(),
            }
        )
        await interaction.response.edit_message(
            content="API 配置\n\n搜索 API 配置已写入 .env，文件监听会自动生效。",
            view=ApiToolboxView(self.bot),
        )


KINK_SEPARATOR = "\n---KINK---\n"


class PromptEditModal(discord.ui.Modal):
    def __init__(self, bot: DiscordBot, *, target: str, title: str):
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
            content="提示词编辑\n\n提示词已保存，下一次调用会自动生效。",
            view=PromptToolboxView(self.bot),
        )


class SoulEditModal(discord.ui.Modal, title="编辑人格 & Kink"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        raw = bot.prompt_service.read_prompt("soul")
        if KINK_SEPARATOR in raw:
            soul_part, kink_part = raw.split(KINK_SEPARATOR, 1)
        else:
            soul_part, kink_part = raw, ""
        self.soul = discord.ui.TextInput(
            label="人格提示词",
            default=soul_part.strip()[:4000],
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.kink = discord.ui.TextInput(
            label="Kink",
            default=kink_part.strip()[:4000],
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000,
        )
        self.add_item(self.soul)
        self.add_item(self.kink)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        soul_text = self.soul.value.strip()
        kink_text = self.kink.value.strip()
        if kink_text:
            combined = f"{soul_text}{KINK_SEPARATOR}{kink_text}"
        else:
            combined = soul_text
        self.bot.prompt_service.write_prompt(target="soul", content=combined)
        await interaction.response.edit_message(
            content="提示词编辑\n\n人格 & Kink 已保存，下一次调用会自动生效。",
            view=PromptToolboxView(self.bot),
        )


class QuietHoursModal(discord.ui.Modal, title="静默时间设置"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        env_values = read_env_values()
        current = bot.settings
        self.enabled = discord.ui.TextInput(
            label="开关（1=开启 0=关闭）",
            default=env_values.get("QUIET_ENABLED", "1" if current.quiet_enabled else "0"),
            required=True,
            max_length=1,
        )
        self.start_time = discord.ui.TextInput(
            label="开始时间（如 23:00）",
            default=env_values.get("QUIET_START", current.quiet_start),
            required=True,
            max_length=5,
        )
        self.end_time = discord.ui.TextInput(
            label="结束时间（如 07:00）",
            default=env_values.get("QUIET_END", current.quiet_end),
            required=True,
            max_length=5,
        )
        self.add_item(self.enabled)
        self.add_item(self.start_time)
        self.add_item(self.end_time)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        update_env_values(
            {
                "QUIET_ENABLED": self.enabled.value.strip(),
                "QUIET_START": self.start_time.value.strip(),
                "QUIET_END": self.end_time.value.strip(),
            }
        )
        await interaction.response.edit_message(
            content="主动消息\n\n静默时间配置已写入 .env，文件监听会自动生效。",
            view=ProactiveToolboxView(self.bot),
        )


class ProactiveModal(discord.ui.Modal, title="聊天主动设置"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        env_values = read_env_values()
        current = bot.settings
        self.idle_seconds = discord.ui.TextInput(
            label="空闲计时（秒），0=关闭",
            default=env_values.get(
                "PROACTIVE_IDLE_SECONDS",
                str(int(current.proactive_idle_seconds)),
            ),
            required=True,
            max_length=10,
        )
        proactive_text = bot.prompt_service.read_prompt("proactive")
        self.prompt = discord.ui.TextInput(
            label="主动发信提示词",
            default=proactive_text[:4000],
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.idle_seconds)
        self.add_item(self.prompt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        update_env_values(
            {"PROACTIVE_IDLE_SECONDS": self.idle_seconds.value.strip()}
        )
        self.bot.prompt_service.write_prompt(
            target="proactive", content=self.prompt.value,
        )
        await self.bot.reload_settings_if_needed()
        await interaction.response.edit_message(
            content="主动消息\n\n聊天主动配置已保存，立即生效。",
            view=ProactiveToolboxView(self.bot),
        )


class WatchOnlineTimeModal(discord.ui.Modal, title="上线监听"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        env_values = read_env_values()
        current = bot.settings
        self.idle_seconds = discord.ui.TextInput(
            label="上线后等待时间（秒），0=关闭",
            default=env_values.get(
                "WATCH_ONLINE_IDLE_SECONDS",
                str(int(current.watch_online_idle_seconds)),
            ),
            required=True,
            max_length=10,
        )
        self.add_item(self.idle_seconds)
        self.prompt = discord.ui.TextInput(
            label="上线提示词（{minutes}=分钟数）",
            style=discord.TextStyle.paragraph,
            default=bot.prompt_service.read_prompt("watch_online").strip()
                or "[系统提示] 你关注的用户已经上线{minutes}分钟了但没有说话，跟他主动说句话。",
            required=False,
            max_length=500,
        )
        self.add_item(self.prompt)
        ids = current.watch_user_ids
        padded = (ids + [""] * 3)[:3]
        self.user_slots: list[discord.ui.TextInput] = []
        for i in range(3):
            inp = discord.ui.TextInput(
                label=f"监听用户 ID {i + 1}",
                default=padded[i],
                required=False,
                max_length=25,
                placeholder="Discord User ID",
            )
            self.user_slots.append(inp)
            self.add_item(inp)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ids = [s.value.strip() for s in self.user_slots if s.value.strip()]
        update_env_values({
            "WATCH_ONLINE_IDLE_SECONDS": self.idle_seconds.value.strip(),
            "WATCH_USER_IDS": ",".join(ids),
        })
        self.bot.prompt_service.write_prompt(
            target="watch_online", content=self.prompt.value,
        )
        await self.bot.reload_settings_if_needed()
        id_list = "\n".join(f"  {uid}" for uid in ids) if ids else "  （无）"
        await interaction.response.edit_message(
            content=(
                f"主动消息\n\n上线等待时间：{self.idle_seconds.value.strip()} 秒\n"
                f"监听用户：\n{id_list}"
            ),
            view=ProactiveToolboxView(self.bot),
        )



class JealousyChannelsModal(discord.ui.Modal, title="频道偷窥"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        ids = bot.settings.jealousy_channel_ids
        padded = (ids + [""] * 5)[:5]
        self.slots: list[discord.ui.TextInput] = []
        for i in range(5):
            inp = discord.ui.TextInput(
                label=f"频道 ID {i + 1}",
                default=padded[i],
                required=False,
                max_length=25,
                placeholder="Discord Channel ID",
            )
            self.slots.append(inp)
            self.add_item(inp)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ids = [s.value.strip() for s in self.slots if s.value.strip()]
        update_env_values({"JEALOUSY_CHANNEL_IDS": ",".join(ids)})
        await self.bot.reload_settings_if_needed()
        id_list = "\n".join(f"  {cid}" for cid in ids) if ids else "  （无）"
        await interaction.response.edit_message(
            content=f"主动消息\n\n偷窥频道已更新：\n{id_list}",
            view=ProactiveToolboxView(self.bot),
        )


class TypingNudgeModal(discord.ui.Modal, title="表情|打字 设置"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        env_values = read_env_values()
        current = bot.settings
        self.nudge_seconds = discord.ui.TextInput(
            label="表情/打字触发等待时间（秒）",
            default=env_values.get(
                "TYPING_NUDGE_SECONDS",
                str(int(current.typing_nudge_seconds)),
            ),
            required=True,
            max_length=10,
        )
        self.add_item(self.nudge_seconds)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        update_env_values(
            {"TYPING_NUDGE_SECONDS": self.nudge_seconds.value.strip()}
        )
        await self.bot.reload_settings_if_needed()
        await interaction.response.edit_message(
            content=f"主动消息\n\n表情|打字等待时间已设为 {self.nudge_seconds.value.strip()} 秒。",
            view=ProactiveToolboxView(self.bot),
        )


# ---------------------------------------------------------------------------
# Sub-panel: API 配置
# ---------------------------------------------------------------------------

class ApiToolboxView(discord.ui.View):
    def __init__(self, bot: DiscordBot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="聊天 API", style=discord.ButtonStyle.primary)
    async def chat_api(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ApiConfigModal(self.bot))

    @discord.ui.button(label="识图 API", style=discord.ButtonStyle.primary)
    async def vision_api(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(VisionConfigModal(self.bot))

    @discord.ui.button(label="搜索 API", style=discord.ButtonStyle.primary)
    async def search_api(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SearchConfigModal(self.bot))

    @discord.ui.button(label="返回", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="工具箱",
            view=ToolboxView(self.bot),
        )


# ---------------------------------------------------------------------------
# Sub-panel: 提示词编辑
# ---------------------------------------------------------------------------

class PromptToolboxView(discord.ui.View):
    def __init__(self, bot: DiscordBot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="人格 & Kink", style=discord.ButtonStyle.primary)
    async def edit_soul(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SoulEditModal(self.bot))

    @discord.ui.button(label="用户信息", style=discord.ButtonStyle.primary)
    async def edit_userinfo(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            PromptEditModal(self.bot, target="userinfo", title="编辑用户信息")
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


# ---------------------------------------------------------------------------
# Sub-panel: 主动消息
# ---------------------------------------------------------------------------

class ProactiveToolboxView(discord.ui.View):
    def __init__(self, bot: DiscordBot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="聊天主动", style=discord.ButtonStyle.primary)
    async def chat_proactive(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ProactiveModal(self.bot))

    @discord.ui.button(label="上线时间", style=discord.ButtonStyle.primary)
    async def watch_online_time(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(WatchOnlineTimeModal(self.bot))

    @discord.ui.button(label="频道偷窥", style=discord.ButtonStyle.primary)
    async def jealousy_channels(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(JealousyChannelsModal(self.bot))

    @discord.ui.button(label="表情|打字", style=discord.ButtonStyle.secondary)
    async def typing_nudge(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(TypingNudgeModal(self.bot))

    @discord.ui.button(label="静默时间", style=discord.ButtonStyle.secondary)
    async def quiet_hours(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(QuietHoursModal(self.bot))

    @discord.ui.button(label="返回", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="工具箱",
            view=ToolboxView(self.bot),
        )


# ---------------------------------------------------------------------------
# Sub-panel: 聊天控制
# ---------------------------------------------------------------------------

class ContextEntriesModal(discord.ui.Modal, title="上下文条数"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        env_values = read_env_values()
        current = bot.settings
        self.entries = discord.ui.TextInput(
            label="主动/闹钟/吃醋等场景的上下文条数",
            default=env_values.get(
                "CONTEXT_ENTRIES",
                str(current.context_entries),
            ),
            required=True,
            max_length=5,
            placeholder="默认 20",
        )
        self.add_item(self.entries)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        update_env_values(
            {"CONTEXT_ENTRIES": self.entries.value.strip()}
        )
        await self.bot.reload_settings_if_needed()
        await interaction.response.edit_message(
            content=f"聊天控制\n\n上下文条数已设为 {self.entries.value.strip()}。",
            view=ChatControlView(self.bot),
        )


class ChatControlView(discord.ui.View):
    def __init__(self, bot: DiscordBot):
        super().__init__(timeout=300)
        self.bot = bot

        # Split mode toggle (no checkmark)
        is_novel = bot.settings.split_mode == "novel"
        split_label = "小说模式" if is_novel else "聊天模式"
        self.split_btn = discord.ui.Button(
            label=split_label,
            style=discord.ButtonStyle.success if is_novel else discord.ButtonStyle.primary,
        )
        self.split_btn.callback = self._toggle_split_mode
        self.add_item(self.split_btn)

        # Typing wait toggle (no checkmark)
        tw = bot.settings.typing_wait
        tw_label = "等待输入" if tw else "即时回复"
        self.tw_btn = discord.ui.Button(
            label=tw_label,
            style=discord.ButtonStyle.primary if tw else discord.ButtonStyle.success,
        )
        self.tw_btn.callback = self._toggle_typing_wait
        self.add_item(self.tw_btn)

    async def _toggle_split_mode(self, interaction: discord.Interaction) -> None:
        current = self.bot.settings.split_mode
        new_mode = "chat" if current == "novel" else "novel"
        update_env_values({"SPLIT_MODE": new_mode})
        await self.bot.reload_settings_if_needed()
        label = "小说模式" if new_mode == "novel" else "聊天模式"
        await interaction.response.edit_message(
            content=f"聊天控制\n\n已切换为{label}",
            view=ChatControlView(self.bot),
        )

    async def _toggle_typing_wait(self, interaction: discord.Interaction) -> None:
        new_val = not self.bot.settings.typing_wait
        update_env_values({"TYPING_WAIT": "1" if new_val else "0"})
        await self.bot.reload_settings_if_needed()
        label = "等待输入" if new_val else "即时回复"
        await interaction.response.edit_message(
            content=f"聊天控制\n\n已切换为{label}",
            view=ChatControlView(self.bot),
        )

    @discord.ui.button(label="上下文条数", style=discord.ButtonStyle.secondary)
    async def context_entries(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ContextEntriesModal(self.bot))

    @discord.ui.button(label="返回", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="工具箱",
            view=ToolboxView(self.bot),
        )


# ---------------------------------------------------------------------------
# Main: 工具箱
# ---------------------------------------------------------------------------

class ToolboxView(discord.ui.View):
    def __init__(self, bot: DiscordBot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="API 配置", style=discord.ButtonStyle.primary)
    async def api_config(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="API 配置",
            view=ApiToolboxView(self.bot),
        )

    @discord.ui.button(label="主动消息", style=discord.ButtonStyle.primary)
    async def proactive(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="主动消息",
            view=ProactiveToolboxView(self.bot),
        )

    @discord.ui.button(label="聊天控制", style=discord.ButtonStyle.primary)
    async def chat_control(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="聊天控制",
            view=ChatControlView(self.bot),
        )

    @discord.ui.button(label="提示词编辑", style=discord.ButtonStyle.secondary)
    async def prompts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="提示词编辑",
            view=PromptToolboxView(self.bot),
        )
