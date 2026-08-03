"""流式解析：把 SSE 分片重新拼成一条完整的 assistant 消息。

流式的难点不是 SSE 协议本身，而是 **tool_calls 是按分片到达的**：

    {"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                               "function": {"name": "write_file", "arguments": "{\\"pa"}}]}}
    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\\": \\"a.py\\"}"}}]}}

`arguments` 是被切碎的 JSON 字符串，中途每一片都不是合法 JSON，只能按 `index` 累积、
等流结束后再整体解析；`id` 与 `name` 通常只在第一片出现，后续分片没有，不能覆盖成空。
多个工具调用会交错到达，`index` 是唯一可靠的归组依据（`id` 可能后到）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from .model import Message, ToolCall

DONE_SENTINEL = "[DONE]"


@dataclass
class _PartialCall:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class StreamAssembler:
    """累积 delta 分片；只在 finish() 时做一次 JSON 解析。"""

    content: str = ""
    finish_reason: str | None = None
    _calls: dict[int, _PartialCall] = field(default_factory=dict)

    def feed(self, payload: dict, on_text: Callable[[str], None] | None = None) -> None:
        if payload.get("error"):
            raise RuntimeError(f"上游返回错误: {json.dumps(payload['error'], ensure_ascii=False)[:500]}")
        for choice in payload.get("choices") or []:
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                self.content += text
                if on_text is not None:
                    on_text(text)
            for fragment in delta.get("tool_calls") or []:
                self._feed_call(fragment)

    def _feed_call(self, fragment: dict) -> None:
        # index 缺失时退化为「按到达顺序追加」，兼容不带 index 的网关
        index = fragment.get("index")
        if index is None:
            index = max(self._calls, default=-1) if self._calls else 0
        partial = self._calls.setdefault(int(index), _PartialCall())
        if fragment.get("id"):
            partial.id = fragment["id"]
        function = fragment.get("function") or {}
        if function.get("name"):
            partial.name = function["name"]
        # 空字符串是合法分片，不能用 or 短路掉
        if function.get("arguments") is not None:
            partial.arguments += function["arguments"]

    def message(self) -> Message:
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            partial = self._calls[index]
            if not partial.name:
                # 有分片但没拿到函数名，说明流被截断，交给上层判定而不是静默丢弃
                raise RuntimeError(f"tool_call#{index} 缺少函数名，流可能被截断")
            try:
                arguments = json.loads(partial.arguments or "{}")
            except json.JSONDecodeError:
                # 与非流式路径保持一致：保留原文让工具层给出可纠正的错误
                arguments = {"_raw": partial.arguments}
            if not isinstance(arguments, dict):
                arguments = {"_raw": partial.arguments}
            calls.append(ToolCall(id=partial.id or f"call_{index}",
                                  name=partial.name, arguments=arguments))
        return Message(role="assistant", content=self.content, tool_calls=calls)

    @property
    def received_any(self) -> bool:
        """是否已经收到过内容——决定断流后能否安全重试（重试会导致重复输出）。"""
        return bool(self.content or self._calls)

    @property
    def partial_call_count(self) -> int:
        return len(self._calls)


def iter_sse_payloads(lines: Iterable[bytes | str]) -> Iterator[dict]:
    """把 SSE 行流转成 JSON 负载；遇到 [DONE] 结束。

    无法解析的 data 行按错误处理而不是跳过：丢掉一个 arguments 分片会得到
    「看起来合法但内容错」的参数，比直接失败更难排查。
    """
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line or line.startswith(":"):  # 空行分隔与心跳注释
            continue
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == DONE_SENTINEL:
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"SSE 分片不是合法 JSON: {data[:200]}") from exc


def assemble(
    lines: Iterable[bytes | str],
    on_text: Callable[[str], None] | None = None,
) -> Message:
    """便捷入口：从 SSE 行流直接得到完整消息。"""
    assembler = StreamAssembler()
    for payload in iter_sse_payloads(lines):
        assembler.feed(payload, on_text=on_text)
    return assembler.message()
