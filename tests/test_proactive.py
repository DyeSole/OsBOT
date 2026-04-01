from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from app.adapters.discord_proactive import ProactiveMixin


class _DummyLogger:
    def info(self, *_args, **_kwargs) -> None:
        pass


class _DummyProactive(ProactiveMixin):
    def __init__(self) -> None:
        self._alarms = {}
        self._variable_timers = {}
        self._pending_alarm_reasons = {}
        self._quiet_buffered_reasons = {}
        self._quiet_channels = {}
        self.logger = _DummyLogger()
        self.sent: list[str] = []

    def _is_quiet_time(self) -> bool:
        return False

    async def _proactive_reply(self, channel_id, channel, system_note, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        self.sent.append(system_note)


class ProactiveAlarmTests(unittest.TestCase):
    def test_alarm_without_reason_uses_generic_text(self) -> None:
        dummy = _DummyProactive()

        asyncio.run(dummy._alarm_fire(1, SimpleNamespace(), 0, None))

        self.assertEqual(len(dummy.sent), 1)
        self.assertIn("闹钟时间到了", dummy.sent[0])
        self.assertNotIn("None", dummy.sent[0])


if __name__ == "__main__":
    unittest.main()
