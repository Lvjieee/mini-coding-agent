from __future__ import annotations

import unittest

from harness.streaming import StreamAssembler, assemble, iter_sse_payloads


def sse(*payloads: str) -> list[str]:
    lines: list[str] = []
    for payload in payloads:
        lines.append(f"data: {payload}")
        lines.append("")
    lines.append("data: [DONE]")
    return lines


def delta(body: str) -> str:
    return '{"choices": [{"index": 0, "delta": ' + body + '}]}'


class SsePayloadTests(unittest.TestCase):
    def test_skips_blank_lines_and_comments(self):
        lines = ["", ": keep-alive", delta_line := "data: " + delta('{"content": "hi"}'), ""]
        self.assertEqual(len(list(iter_sse_payloads(lines))), 1)
        self.assertIn("hi", delta_line)

    def test_stops_at_done_sentinel(self):
        lines = ["data: " + delta('{"content": "a"}'), "data: [DONE]",
                 "data: " + delta('{"content": "b"}')]
        payloads = list(iter_sse_payloads(lines))
        self.assertEqual(len(payloads), 1)

    def test_accepts_bytes(self):
        lines = [b"data: " + delta('{"content": "hi"}').encode(), b"data: [DONE]"]
        self.assertEqual(assemble(lines).content, "hi")

    def test_malformed_json_is_reported(self):
        with self.assertRaises(RuntimeError) as ctx:
            list(iter_sse_payloads(["data: {not json"]))
        self.assertIn("合法 JSON", str(ctx.exception))


class AssemblerTests(unittest.TestCase):
    def test_text_chunks_stream_in_order(self):
        seen: list[str] = []
        message = assemble(
            sse(delta('{"content": "Hel"}'), delta('{"content": "lo"}')),
            on_text=seen.append,
        )
        self.assertEqual(message.content, "Hello")
        self.assertEqual(seen, ["Hel", "lo"])

    def test_tool_call_arguments_are_split_across_chunks(self):
        message = assemble(sse(
            delta('{"tool_calls": [{"index": 0, "id": "call_1",'
                  ' "function": {"name": "write_file", "arguments": "{\\"pa"}}]}'),
            delta('{"tool_calls": [{"index": 0,'
                  ' "function": {"arguments": "th\\": \\"a.py\\"}"}}]}'),
        ))
        self.assertEqual(len(message.tool_calls), 1)
        call = message.tool_calls[0]
        self.assertEqual(call.id, "call_1")
        self.assertEqual(call.name, "write_file")
        self.assertEqual(call.arguments, {"path": "a.py"})

    def test_interleaved_tool_calls_are_grouped_by_index(self):
        message = assemble(sse(
            delta('{"tool_calls": [{"index": 0, "id": "a",'
                  ' "function": {"name": "read_file", "arguments": "{\\"path\\":"}}]}'),
            delta('{"tool_calls": [{"index": 1, "id": "b",'
                  ' "function": {"name": "list_dir", "arguments": "{\\"path\\":"}}]}'),
            delta('{"tool_calls": [{"index": 1, "function": {"arguments": " \\"src\\"}"}}]}'),
            delta('{"tool_calls": [{"index": 0, "function": {"arguments": " \\"a.py\\"}"}}]}'),
        ))
        self.assertEqual([call.name for call in message.tool_calls],
                         ["read_file", "list_dir"])
        self.assertEqual(message.tool_calls[0].arguments, {"path": "a.py"})
        self.assertEqual(message.tool_calls[1].arguments, {"path": "src"})

    def test_later_fragments_do_not_erase_id_or_name(self):
        message = assemble(sse(
            delta('{"tool_calls": [{"index": 0, "id": "call_9",'
                  ' "function": {"name": "run_command", "arguments": ""}}]}'),
            delta('{"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}'),
        ))
        self.assertEqual(message.tool_calls[0].id, "call_9")
        self.assertEqual(message.tool_calls[0].name, "run_command")

    def test_truncated_arguments_fall_back_to_raw(self):
        message = assemble(sse(
            delta('{"tool_calls": [{"index": 0, "id": "call_1",'
                  ' "function": {"name": "write_file", "arguments": "{\\"path\\": \\"a"}}]}'),
        ))
        self.assertEqual(message.tool_calls[0].arguments, {"_raw": '{"path": "a'})

    def test_fragment_without_name_is_an_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            assemble(sse(delta('{"tool_calls": [{"index": 0,'
                               ' "function": {"arguments": "{}"}}]}')))
        self.assertIn("缺少函数名", str(ctx.exception))

    def test_upstream_error_payload_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            assemble(['data: {"error": {"message": "rate limited"}}'])
        self.assertIn("rate limited", str(ctx.exception))

    def test_received_any_tracks_progress(self):
        assembler = StreamAssembler()
        self.assertFalse(assembler.received_any)
        assembler.feed({"choices": [{"delta": {"content": "x"}}]})
        self.assertTrue(assembler.received_any)

    def test_finish_reason_is_captured(self):
        assembler = StreamAssembler()
        assembler.feed({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        self.assertEqual(assembler.finish_reason, "tool_calls")


if __name__ == "__main__":
    unittest.main()
