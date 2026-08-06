from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from harness import OpenAICompatClient


class ExtractMessageTests(unittest.TestCase):
    """网关返回 HTTP 200 + 错误体时，必须把服务端原文带进报错。

    背景：一次 27 连挂的批量评测只看到 `KeyError: 'choices'`，
    服务端真正说的话（路径错 / 模型名错 / 额度用尽）全被吞掉，无法定位。
    """

    def setUp(self):
        self.client = OpenAICompatClient(
            model="glm-4-flash", base_url="https://example.test/api/wrong/path",
            api_key="dummy")

    def test_missing_choices_surfaces_server_body(self):
        payload = {"error": {"code": "1002", "message": "API key 无效"}}

        with self.assertRaises(RuntimeError) as ctx:
            self.client._extract_message(payload)

        text = str(ctx.exception)
        self.assertIn("API key 无效", text)
        self.assertIn("wrong/path", text, "报错应带上 base_url 便于定位配置问题")
        self.assertIn("glm-4-flash", text)

    def test_empty_choices_list_is_an_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.client._extract_message({"choices": []})
        self.assertIn("缺少 choices", str(ctx.exception))

    def test_choice_without_message_is_an_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.client._extract_message({"choices": [{"finish_reason": "stop"}]})
        self.assertIn("缺少 message", str(ctx.exception))

    def test_valid_response_returns_message(self):
        message = self.client._extract_message(
            {"choices": [{"message": {"content": "hi", "role": "assistant"}}]})
        self.assertEqual(message["content"], "hi")

    def test_non_dict_payload_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.client._extract_message([])


if __name__ == "__main__":
    unittest.main()
