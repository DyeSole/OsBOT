"""Discord UI components – modals, views, and buttons for the toolbox."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from app.config.settings import read_env_values, update_env_values

if TYPE_CHECKING:
    from app.adapters.discord_bot import DiscordBot


class ApiConfigModal(discord.ui.Modal, title="编辑 API 配置"):
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
            content="工具箱\n\nAPI 配置已写入 .env，文件监听会自动生效。",
            view=ToolboxView(self.bot),
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
            content="提示词工具箱\n\n提示词已保存，下一次调用会自动生效。",
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
            content="提示词工具箱\n\n人格 & Kink 已保存，下一次调用会自动生效。",
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
            content="工具箱\n\n静默时间配置已写入 .env，文件监听会自动生效。",
            view=ToolboxView(self.bot),
        )


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


class ToolboxView(discord.ui.View):
    def __init__(self, bot: DiscordBot):
        super().__init__(timeout=300)
        self.bot = bot
        # Split mode toggle
        is_novel = bot.settings.split_mode == "novel"
        split_label = "小说模式 ✓" if is_novel else "聊天模式 ✓"
        self.split_btn = discord.ui.Button(
            label=split_label,
            style=discord.ButtonStyle.success if is_novel else discord.ButtonStyle.primary,
        )
        self.split_btn.callback = self._toggle_split_mode
        self.add_item(self.split_btn)

        # Typing wait toggle
        tw = bot.settings.typing_wait
        tw_label = "等待输入 ✓" if tw else "即时回复 ✓"
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
            content=f"工具箱\n\n已切换为{label}",
            view=ToolboxView(self.bot),
        )

    async def _toggle_typing_wait(self, interaction: discord.Interaction) -> None:
        new_val = not self.bot.settings.typing_wait
        update_env_values({"TYPING_WAIT": "1" if new_val else "0"})
        await self.bot.reload_settings_if_needed()
        label = "等待输入" if new_val else "即时回复"
        await interaction.response.edit_message(
            content=f"工具箱\n\n已切换为{label}",
            view=ToolboxView(self.bot),
        )

    @discord.ui.button(label="API配置", style=discord.ButtonStyle.primary)
    async def api_config(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ApiConfigModal(self.bot))

    @discord.ui.button(label="静默时间", style=discord.ButtonStyle.secondary)
    async def quiet_hours(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(QuietHoursModal(self.bot))

    @discord.ui.button(label="提示词", style=discord.ButtonStyle.secondary)
    async def prompts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="提示词工具箱",
            view=PromptToolboxView(self.bot),
        )
