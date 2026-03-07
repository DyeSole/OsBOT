# OsBOT

A clean, independent Discord emotional companion bot project.

## Architecture

- `app/adapters/`: platform entrypoints (Discord)
- `app/services/`: business orchestration (reply/emotion/memory/proactive)
- `app/core/`: domain rules, policy, persona, safety
- `app/infra/`: external clients and persistence
- `app/config/`: env loading and validation
- `prompts/`: persona and prompt assets
- `data/`: runtime data (history/state/memory)
- `tests/`: unit and flow tests
- `scripts/`: run/dev utility scripts
- `docs/`: technical docs

## Environment

Use `OsBOT/.env` directly:

- `DISCORD_BOT_TOKEN=` (required)
- `BOT_KEY=Haze` (optional, default: `Haze`)
- `APP_MODE=normal` (optional: `normal` or `debug`)
- `SHOW_ERROR_DETAIL=false` (optional, `true` shows both `🚨 UNKNOWN` and `🔍 DEBUG`)
- `SHOW_API_PAYLOAD=false` (optional, `true` prints the final transcript sent to API)
- `SESSION_TIMEOUT_SECONDS=15.0` (optional, shared by typing timeout and max wait)
- `TYPING_DETECT_DELAY_SECONDS=1.0` (optional, global quiet window before API request; any new message/typing resets it)
- `RESET_TIMER_SECONDS=2.5` (optional, quiet window used by reset timer before sending API)
- `BASE_URL=https://clewdr.omenaros.site/code/v1` (required)
- `API_KEY=` (required)
- `MODEL=claude-4.6-opus` (optional)

## Run

```bash
cd OsBOT
python3 -m pip install -r requirements.txt
python3 bot.py
```

## Notes

This project is intentionally isolated from `DcBot` and does not import its code.
