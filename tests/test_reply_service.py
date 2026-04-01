from __future__ import annotations

import unittest

from app.services.reply_service import parse_tool_tags


class ReplyServiceToolTagTests(unittest.TestCase):
    def test_parse_chinese_tool_tags(self) -> None:
        text, calls = parse_tool_tags(
            "[计时器: 30]\n[闹钟: 18:30 | 吃药]\n[表情反应: ❤️]\n[画图: cat]\n[语音: hi]\n[搜索: codex]"
        )

        self.assertEqual(text, "")
        self.assertEqual(
            [(call.name, call.input) for call in calls],
            [
                ("set_timer", {"seconds": 30.0}),
                ("set_alarm", {"seconds": calls[1].input["seconds"], "reason": "吃药"}),
                ("add_reaction", {"emoji": "❤️"}),
                ("generate_image", {"prompt": "cat"}),
                ("send_voice", {"text": "hi"}),
                ("web_search", {"query": "codex"}),
            ],
        )
        self.assertGreater(calls[1].input["seconds"], 0)

    def test_parse_chinese_switch_mode(self) -> None:
        text, calls = parse_tool_tags("[切换模式: 小说]")
        self.assertEqual(text, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "switch_mode")
        self.assertEqual(calls[0].input, {"mode": "novel"})

    def test_alarm_requires_reason(self) -> None:
        text, calls = parse_tool_tags("[闹钟: 18:30]")
        self.assertEqual(text, "")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
