from __future__ import annotations

from app.adapters.discord_bot import DiscordBot
from app.config.settings import load_settings


def main() -> None:
    settings = load_settings()
    bot = DiscordBot(settings)
    bot.run_forever()


if __name__ == "__main__":
    main()
