#!/usr/bin/env bash
set -e
# Kill any existing Xvfb on :99
pkill -f "Xvfb :99" 2>/dev/null || true
# Start Xvfb
Xvfb :99 -screen 0 1280x800x24 -ac &
XVFB_PID=$!
export DISPLAY=:99
sleep 1
# Start bot
cd /home/weawer/OsBOT
exec /home/weawer/OsBOT/.venv/bin/python bot.py
