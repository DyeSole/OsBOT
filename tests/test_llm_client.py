from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.infra.llm_client import LLMClient


class LLMClientTokenCountTests(unittest.TestCase):
    @patch("app.infra.llm_client.requests.post")
    def test_count_input_tokens_uses_remote_anthropic_count_when_available(self, mock_post: Mock) -> None:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"input_tokens": 321}
        mock_post.return_value = mock_response

        client = LLMClient(
            base_url="https://example.com/v1/messages",
            api_key="secret",
            model="claude-test",
        )

        count = client.count_input_tokens(
            messages=[{"role": "user", "content": "hello"}],
            system_prompt="sys",
            tools=[],
        )

        self.assertEqual(count, 321)
        mock_post.assert_called_once()
        self.assertEqual(
            mock_post.call_args.kwargs["json"],
            {
                "model": "claude-test",
                "messages": [{"role": "user", "content": "hello"}],
                "system": "sys",
            },
        )


if __name__ == "__main__":
    unittest.main()
