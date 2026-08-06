"""模型层：把模型当作无状态函数。

输出 = 模型(系统提示词 + 工具 + 会话历史 + 其他上下文 + 用户指令)

模型不保存任何状态；对话历史、记忆、任务进度全部由 harness 维护并按需注入。
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Message:
    role: str  # system / user / assistant / tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None

    def to_api(self) -> dict:
        msg: dict = {"role": self.role, "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        return msg


class ModelClient(Protocol):
    def complete(self, messages: list[Message], tools: list[dict]) -> Message: ...


class OpenAICompatClient:
    """任何 OpenAI 兼容接口（含各国产模型网关）均可使用。"""

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.2,
        max_retries: int = 3,
    ):
        self.model = model
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.temperature = temperature
        self.max_retries = max_retries

    def _build_request(self, payload: dict) -> urllib.request.Request:
        return urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

    def _http_error_detail(self, error: urllib.error.HTTPError) -> str:
        """把服务端返回的错误体带出来，否则只能看到无信息的 "400 Bad Request"。"""
        try:
            return error.read().decode("utf-8", "replace")[:800]
        except Exception:
            return ""

    def _extract_message(self, data: dict) -> dict:
        """取出 choices[0].message，缺失时把响应体原文带进报错。

        有些网关对错误请求（路径写错、模型名不存在、额度用尽）返回 HTTP 200 +
        错误体，此时直接索引 `choices` 会抛 `KeyError: 'choices'`，
        把服务端真正说的话整个吞掉——批量评测会看到 N 次同样的无信息报错。
        """
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            body = json.dumps(data, ensure_ascii=False)[:800]
            raise RuntimeError(
                f"响应缺少 choices（base_url={self.base_url} model={self.model}）；"
                f"服务端返回：{body}")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            body = json.dumps(choices[0], ensure_ascii=False)[:800]
            raise RuntimeError(f"响应 choices[0] 缺少 message；服务端返回：{body}")
        return message

    def complete(self, messages: list[Message], tools: list[dict]) -> Message:
        payload: dict = {
            "model": self.model,
            "messages": [m.to_api() for m in messages],
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
        req = self._build_request(payload)
        # 网关偶发读超时/瞬时错误时指数退避重试，避免整批评测因单次抖动中断。
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                # 4xx 是请求本身的问题（模型名/参数/鉴权），重试无意义
                body = self._http_error_detail(e)
                if 400 <= e.code < 500:
                    raise RuntimeError(f"HTTP {e.code} from {self.base_url} (model={self.model}): {body}") from e
                last_err = e
                if attempt == self.max_retries - 1:
                    raise RuntimeError(f"HTTP {e.code}: {body}") from e
                time.sleep(2 ** attempt)
            except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
                last_err = e
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
        else:  # pragma: no cover
            raise last_err  # type: ignore[misc]
        raw = self._extract_message(data)
        calls: list[ToolCall] = []
        for tc in raw.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc["function"].get("arguments")}
            calls.append(ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=args))
        return Message(role="assistant", content=raw.get("content") or "", tool_calls=calls)

    def complete_stream(
        self,
        messages: list[Message],
        tools: list[dict],
        on_text=None,
    ) -> Message:
        """流式版本：边收边把文本交给 on_text，结束后返回拼装好的完整消息。

        与非流式的关键差别在重试语义：一旦收到过分片就**不能**重试，
        否则用户会看到重复输出、工具调用也可能被执行两次。
        """
        from .streaming import StreamAssembler, iter_sse_payloads

        payload: dict = {
            "model": self.model,
            "messages": [m.to_api() for m in messages],
            "temperature": self.temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        for attempt in range(self.max_retries):
            assembler = StreamAssembler()
            try:
                with urllib.request.urlopen(self._build_request(payload), timeout=600) as resp:
                    for chunk in iter_sse_payloads(resp):
                        assembler.feed(chunk, on_text=on_text)
                if not assembler.received_any:
                    # 一个分片都没有：网关很可能返回了 200 + 错误体或空流，
                    # 静默返回空消息会让上层误判为「模型什么都没说」
                    raise RuntimeError(
                        f"流式响应为空（base_url={self.base_url} model={self.model}）；"
                        "检查 base_url 路径、模型名与额度。")
                return assembler.message()
            except urllib.error.HTTPError as e:
                body = self._http_error_detail(e)
                if 400 <= e.code < 500:
                    raise RuntimeError(
                        f"HTTP {e.code} from {self.base_url} (model={self.model}): {body}") from e
                if attempt == self.max_retries - 1:
                    raise RuntimeError(f"HTTP {e.code}: {body}") from e
                time.sleep(2 ** attempt)
            except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
                # 断在中途：已经吐出去的内容无法撤回，重试会重复，直接上抛
                if assembler.received_any:
                    raise RuntimeError(
                        f"流式响应中断（已收到 {len(assembler.content)} 字符，"
                        f"{assembler.partial_call_count} 个工具调用分片），不重试以避免重复执行"
                    ) from e
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError("unreachable")  # pragma: no cover
