"""Discord UI components: modals, views, and toolbox panels."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from app.config.settings import save_config, load_settings

if TYPE_CHECKING:
    from app.adapters.discord_bot import DiscordBot


# ============================================================================
#  Base classes
# ============================================================================

class BotView(discord.ui.View):
    """Base view that stores a bot reference and sets a uniform timeout."""

    def __init__(self, bot: DiscordBot):
        super().__init__(timeout=300)
        self.bot = bot


# ============================================================================
#  Data-driven config modal
# ============================================================================

@dataclass
class FieldDef:
    key: str
    label: str
    required: bool = True
    placeholder: str = ""
    max_length: int = 400
    style: discord.TextStyle = discord.TextStyle.short


def _settings_value(settings, key: str) -> str:
    """Read current value from Settings, converting to display string."""
    attr = key.lower()
    val = getattr(settings, attr, "")
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, list):
        return ",".join(val)
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val)


class ConfigModal(discord.ui.Modal):
    """Generic modal that renders FieldDefs, saves to config.json, and applies."""

    def __init__(
        self,
        bot: DiscordBot,
        *,
        fields: list[FieldDef],
        title: str,
        confirm: str,
        return_view: type[BotView],
    ):
        super().__init__(title=title)
        self.bot = bot
        self._confirm = confirm
        self._return_view = return_view
        self._inputs: dict[str, discord.ui.TextInput] = {}

        for f in fields:
            inp = discord.ui.TextInput(
                label=f.label,
                default=_settings_value(bot.settings, f.key),
                required=f.required,
                placeholder=f.placeholder or None,
                max_length=f.max_length,
                style=f.style,
            )
            self._inputs[f.key] = inp
            self.add_item(inp)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        updates = {k: inp.value.strip() for k, inp in self._inputs.items()}
        save_config(updates)
        self.bot.apply_settings(load_settings())
        await interaction.response.edit_message(
            content=self._confirm,
            view=self._return_view(self.bot),
        )


# ============================================================================
#  Field definitions per panel
# ============================================================================

CHAT_API_FIELDS = [
    FieldDef("BASE_URL", "BASE_URL"),
    FieldDef("API_KEY", "API_KEY"),
    FieldDef("MODEL", "MODEL", max_length=120),
]


SEARCH_API_FIELDS = [
    FieldDef("SEARCH_BASE_URL", "SEARCH_BASE_URL", required=False, placeholder="留空则使用聊天 API"),
    FieldDef("SEARCH_API_KEY", "SEARCH_API_KEY", required=False, placeholder="留空则使用聊天 API"),
    FieldDef("SEARCH_MODEL", "SEARCH_MODEL", required=False, max_length=120, placeholder="留空则使用duckduckgo"),
]

COMPRESSION_API_FIELDS = [
    FieldDef("COMPRESSION_BASE_URL", "COMPRESSION_BASE_URL", required=False, placeholder="留空则使用聊天 API"),
    FieldDef("COMPRESSION_API_KEY", "COMPRESSION_API_KEY", required=False, placeholder="留空则使用聊天 API"),
    FieldDef("COMPRESSION_MODEL", "COMPRESSION_MODEL", required=False, max_length=120, placeholder="留空则使用聊天 API"),
]

TTS_FIELDS = [
    FieldDef("TTS_API_KEY", "TTS_API_KEY", required=False, placeholder="MiniMax API Key"),
    FieldDef("TTS_VOICE_ID", "TTS_VOICE_ID", required=False, placeholder="克隆音色 ID"),
    FieldDef("TTS_SPEED", "语速（0.5~2.0，默认 1.0）", required=False, max_length=5, placeholder="1.0"),
    FieldDef("TTS_PITCH", "音调（-12~12，默认 0）", required=False, max_length=4, placeholder="0"),
    FieldDef("TTS_EMOTION", "情绪（happy/sad/angry/neutral 等）", required=False, max_length=20, placeholder="留空则不设"),
]

PIXAI_FIELDS: list[FieldDef] = []  # replaced by PixAITokenModal

QUIET_HOURS_FIELDS = [
    FieldDef("QUIET_ENABLED", "开关（1=开启 0=关闭）", max_length=1),
    FieldDef("QUIET_START", "开始时间（如 23:00）", max_length=5),
    FieldDef("QUIET_END", "结束时间（如 07:00）", max_length=5),
]

TYPING_NUDGE_FIELDS = [
    FieldDef("TYPING_NUDGE_SECONDS", "表情/打字触发等待时间（秒）", max_length=10),
]

CONTEXT_ENTRIES_FIELDS = [
    FieldDef("CONTEXT_ENTRIES", "主动/闹钟/吃醋等场景的上下文条数", max_length=5, placeholder="默认 20"),
]


# ============================================================================
#  Special modals (prompt editing, dynamic slots)
# ============================================================================

KINK_SEPARATOR = "\n---KINK---\n"


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
        combined = f"{soul_text}{KINK_SEPARATOR}{kink_text}" if kink_text else soul_text
        self.bot.prompt_service.write_prompt(target="soul", content=combined)
        await interaction.response.edit_message(
            content="提示词编辑\n\n人格 & Kink 已保存，下一次调用会自动生效。",
            view=PromptToolboxView(self.bot),
        )


class PromptEditModal(discord.ui.Modal):
    def __init__(self, bot: DiscordBot, *, target: str, title: str):
        super().__init__(title=title)
        self.bot = bot
        self.target = target
        self.content = discord.ui.TextInput(
            label=title,
            default=bot.prompt_service.read_prompt(target)[:4000],
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.bot.prompt_service.write_prompt(target=self.target, content=self.content.value)
        await interaction.response.edit_message(
            content="提示词编辑\n\n提示词已保存，下一次调用会自动生效。",
            view=PromptToolboxView(self.bot),
        )


class ProactiveModal(discord.ui.Modal, title="聊天主动设置"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        self.idle_seconds = discord.ui.TextInput(
            label="空闲计时（秒），0=关闭",
            default=_settings_value(bot.settings, "PROACTIVE_IDLE_SECONDS"),
            required=True,
            max_length=10,
        )
        self.prompt = discord.ui.TextInput(
            label="主动发信提示词",
            default=bot.prompt_service.read_prompt("proactive")[:4000],
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.idle_seconds)
        self.add_item(self.prompt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        save_config({"PROACTIVE_IDLE_SECONDS": self.idle_seconds.value.strip()})
        self.bot.prompt_service.write_prompt(target="proactive", content=self.prompt.value)
        self.bot.apply_settings(load_settings())
        await interaction.response.edit_message(
            content="主动消息\n\n聊天主动配置已保存，立即生效。",
            view=ProactiveToolboxView(self.bot),
        )


class CompressionModal(discord.ui.Modal, title="压缩 API 配置"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        s = bot.settings
        self.base_url = discord.ui.TextInput(
            label="COMPRESSION_BASE_URL",
            default=s.compression_base_url or "",
            required=False, placeholder="留空则使用聊天 API",
        )
        self.add_item(self.base_url)
        self.api_key = discord.ui.TextInput(
            label="COMPRESSION_API_KEY",
            default=s.compression_api_key or "",
            required=False, placeholder="留空则使用聊天 API",
        )
        self.add_item(self.api_key)
        self.model = discord.ui.TextInput(
            label="COMPRESSION_MODEL",
            default=s.compression_model or "",
            required=False, max_length=120, placeholder="留空则使用聊天 API",
        )
        self.add_item(self.model)
        self.compression_prompt = discord.ui.TextInput(
            label="压缩提示词",
            style=discord.TextStyle.paragraph,
            default=bot.prompt_service.read_prompt("compression").strip(),
            required=False,
            max_length=2000,
            placeholder="请输出 JSON，包含 summary_text 和 keywords 两个字段。",
        )
        self.add_item(self.compression_prompt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        save_config({
            "COMPRESSION_BASE_URL": self.base_url.value.strip(),
            "COMPRESSION_API_KEY": self.api_key.value.strip(),
            "COMPRESSION_MODEL": self.model.value.strip(),
        })
        self.bot.prompt_service.write_prompt(target="compression", content=self.compression_prompt.value)
        self.bot.apply_settings(load_settings())
        await interaction.response.edit_message(
            content="API 配置\n\n压缩 API 配置已保存，立即生效。",
            view=ApiToolboxView(self.bot),
        )


class VisionModal(discord.ui.Modal, title="识图 API 配置"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        s = bot.settings
        self.base_url = discord.ui.TextInput(
            label="VISION_BASE_URL",
            default=s.vision_base_url or "",
            required=False, placeholder="留空则使用聊天 API",
        )
        self.add_item(self.base_url)
        self.api_key = discord.ui.TextInput(
            label="VISION_API_KEY",
            default=s.vision_api_key or "",
            required=False, placeholder="留空则使用聊天 API",
        )
        self.add_item(self.api_key)
        self.model = discord.ui.TextInput(
            label="VISION_MODEL",
            default=s.vision_model or "",
            required=False, max_length=120, placeholder="留空则使用聊天 API",
        )
        self.add_item(self.model)
        self.vision_prompt = discord.ui.TextInput(
            label="识图提示词",
            style=discord.TextStyle.paragraph,
            default=bot.prompt_service.read_prompt("vision").strip(),
            required=False,
            max_length=800,
        )
        self.add_item(self.vision_prompt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        save_config({
            "VISION_BASE_URL": self.base_url.value.strip(),
            "VISION_API_KEY": self.api_key.value.strip(),
            "VISION_MODEL": self.model.value.strip(),
        })
        self.bot.prompt_service.write_prompt(target="vision", content=self.vision_prompt.value)
        self.bot.apply_settings(load_settings())
        await interaction.response.edit_message(
            content="API 配置\n\n识图 API 配置已保存，立即生效。",
            view=ApiToolboxView(self.bot),
        )


class PixAITokenModal(discord.ui.Modal, title="画图 API 配置"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        tokens = bot.settings.pixai_tokens
        padded = (tokens + [""] * 4)[:4]
        self.token_slots: list[discord.ui.TextInput] = []
        for i in range(4):
            inp = discord.ui.TextInput(
                label=f"Token {i + 1}",
                default=padded[i],
                required=False,
                max_length=800,
                style=discord.TextStyle.short,
                placeholder="PixAI JWT Token",
            )
            self.token_slots.append(inp)
            self.add_item(inp)
        self.pixai_prompt = discord.ui.TextInput(
            label="画图提示词（自动追加到用户 prompt 后）",
            style=discord.TextStyle.paragraph,
            default=bot.prompt_service.read_prompt("pixai").strip(),
            required=False,
            max_length=2000,
            placeholder="masterpiece, best quality, high detail",
        )
        self.add_item(self.pixai_prompt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tokens = [s.value.strip() for s in self.token_slots if s.value.strip()]
        save_config({"PIXAI_TOKENS": ",".join(tokens)})
        self.bot.prompt_service.write_prompt(target="pixai", content=self.pixai_prompt.value)
        self.bot.apply_settings(load_settings())
        await interaction.response.edit_message(
            content=f"API 配置\n\n画图 API 配置已保存，共 {len(tokens)} 个 Token，立即生效。",
            view=ApiToolboxView(self.bot),
        )


class WatchOnlineTimeModal(discord.ui.Modal, title="上线监听"):
    def __init__(self, bot: DiscordBot):
        super().__init__()
        self.bot = bot
        self.idle_seconds = discord.ui.TextInput(
            label="上线后等待时间（秒），0=关闭",
            default=_settings_value(bot.settings, "WATCH_ONLINE_IDLE_SECONDS"),
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
        ids = bot.settings.watch_user_ids
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
        save_config({
            "WATCH_ONLINE_IDLE_SECONDS": self.idle_seconds.value.strip(),
            "WATCH_USER_IDS": ",".join(ids),
        })
        self.bot.prompt_service.write_prompt(target="watch_online", content=self.prompt.value)
        self.bot.apply_settings(load_settings())
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
        save_config({"JEALOUSY_CHANNEL_IDS": ",".join(ids)})
        self.bot.apply_settings(load_settings())
        id_list = "\n".join(f"  {cid}" for cid in ids) if ids else "  （无）"
        await interaction.response.edit_message(
            content=f"主动消息\n\n偷窥频道已更新：\n{id_list}",
            view=ProactiveToolboxView(self.bot),
        )


# ============================================================================
#  Views
# ============================================================================

class ApiToolboxView(BotView):
    @discord.ui.button(label="聊天 API", style=discord.ButtonStyle.primary, row=0)
    async def chat_api(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ConfigModal(
            self.bot, fields=CHAT_API_FIELDS, title="聊天 API 配置",
            confirm="API 配置\n\n聊天 API 配置已保存，立即生效。", return_view=ApiToolboxView,
        ))

    @discord.ui.button(label="识图 API", style=discord.ButtonStyle.primary, row=0)
    async def vision_api(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(VisionModal(self.bot))

    @discord.ui.button(label="搜索 API", style=discord.ButtonStyle.primary, row=0)
    async def search_api(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ConfigModal(
            self.bot, fields=SEARCH_API_FIELDS, title="搜索 API 配置",
            confirm="API 配置\n\n搜索 API 配置已保存，立即生效。", return_view=ApiToolboxView,
        ))

    @discord.ui.button(label="语音 API", style=discord.ButtonStyle.primary, row=1)
    async def tts_config(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ConfigModal(
            self.bot, fields=TTS_FIELDS, title="语音 API 配置",
            confirm="API 配置\n\n语音 API 配置已保存，立即生效。", return_view=ApiToolboxView,
        ))

    @discord.ui.button(label="压缩 API", style=discord.ButtonStyle.primary, row=1)
    async def compression_api(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CompressionModal(self.bot))

    @discord.ui.button(label="画图 API", style=discord.ButtonStyle.primary, row=1)
    async def pixai_config(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PixAITokenModal(self.bot))

    @discord.ui.button(label="返回", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="工具箱", view=ToolboxView(self.bot))


class PromptToolboxView(BotView):
    @discord.ui.button(label="人格 & Kink", style=discord.ButtonStyle.primary)
    async def edit_soul(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SoulEditModal(self.bot))

    @discord.ui.button(label="用户信息", style=discord.ButtonStyle.primary)
    async def edit_userinfo(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PromptEditModal(self.bot, target="userinfo", title="编辑用户信息"))

    @discord.ui.button(label="小说模式提示词", style=discord.ButtonStyle.primary)
    async def edit_novel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PromptEditModal(self.bot, target="novel", title="编辑小说模式提示词"))

    @discord.ui.button(label="返回", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="工具箱", view=ToolboxView(self.bot))


class ProactiveToolboxView(BotView):
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
        await interaction.response.send_modal(ConfigModal(
            self.bot, fields=TYPING_NUDGE_FIELDS, title="表情|打字 设置",
            confirm="主动消息\n\n表情|打字等待时间已保存，立即生效。", return_view=ProactiveToolboxView,
        ))

    @discord.ui.button(label="静默时间", style=discord.ButtonStyle.secondary)
    async def quiet_hours(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ConfigModal(
            self.bot, fields=QUIET_HOURS_FIELDS, title="静默时间设置",
            confirm="主动消息\n\n静默时间配置已保存，立即生效。", return_view=ProactiveToolboxView,
        ))

    @discord.ui.button(label="返回", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="工具箱", view=ToolboxView(self.bot))


class ChatControlView(BotView):
    def __init__(self, bot: DiscordBot):
        super().__init__(bot)

        mode = bot.settings.split_mode
        mode_labels = {"chat": "聊天模式", "novel": "小说模式", "auto": "LLM模式"}
        mode_styles = {
            "chat": discord.ButtonStyle.primary,
            "novel": discord.ButtonStyle.success,
            "auto": discord.ButtonStyle.secondary,
        }
        self.split_btn = discord.ui.Button(
            label=mode_labels.get(mode, "LLM模式"),
            style=mode_styles.get(mode, discord.ButtonStyle.secondary),
        )
        self.split_btn.callback = self._toggle_split_mode
        self.add_item(self.split_btn)

        tw = bot.settings.typing_wait
        self.tw_btn = discord.ui.Button(
            label="等待输入" if tw else "即时回复",
            style=discord.ButtonStyle.primary if tw else discord.ButtonStyle.success,
        )
        self.tw_btn.callback = self._toggle_typing_wait
        self.add_item(self.tw_btn)

    async def _toggle_split_mode(self, interaction: discord.Interaction) -> None:
        cycle = ["chat", "novel", "auto"]
        current = self.bot.settings.split_mode
        idx = cycle.index(current) if current in cycle else 0
        new_mode = cycle[(idx + 1) % len(cycle)]
        save_config({"SPLIT_MODE": new_mode})
        self.bot.apply_settings(load_settings())
        mode_labels = {"chat": "聊天模式", "novel": "小说模式", "auto": "LLM模式"}
        await interaction.response.edit_message(
            content=f"聊天控制\n\n已切换为{mode_labels[new_mode]}",
            view=ChatControlView(self.bot),
        )

    async def _toggle_typing_wait(self, interaction: discord.Interaction) -> None:
        new_val = not self.bot.settings.typing_wait
        save_config({"TYPING_WAIT": "1" if new_val else "0"})
        self.bot.apply_settings(load_settings())
        await interaction.response.edit_message(
            content=f"聊天控制\n\n已切换为{'等待输入' if new_val else '即时回复'}",
            view=ChatControlView(self.bot),
        )

    @discord.ui.button(label="上下文条数", style=discord.ButtonStyle.secondary, row=0)
    async def context_entries(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ConfigModal(
            self.bot, fields=CONTEXT_ENTRIES_FIELDS, title="上下文条数",
            confirm="聊天控制\n\n上下文条数已保存，立即生效。", return_view=ChatControlView,
        ))

    @discord.ui.button(label="返回", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="工具箱", view=ToolboxView(self.bot))


class ToolboxView(BotView):
    @discord.ui.button(label="API 配置", style=discord.ButtonStyle.primary)
    async def api_config(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="API 配置", view=ApiToolboxView(self.bot))

    @discord.ui.button(label="主动消息", style=discord.ButtonStyle.primary)
    async def proactive(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="主动消息", view=ProactiveToolboxView(self.bot))

    @discord.ui.button(label="聊天控制", style=discord.ButtonStyle.primary)
    async def chat_control(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="聊天控制", view=ChatControlView(self.bot))

    @discord.ui.button(label="提示词编辑", style=discord.ButtonStyle.secondary)
    async def prompts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="提示词编辑", view=PromptToolboxView(self.bot))
