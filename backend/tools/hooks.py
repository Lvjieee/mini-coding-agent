"""Lifecycle hooks (harness 组件)。

在工具执行的关键节点（PreToolUse / PostToolUse）插入可拦截的检查层。
PreToolUse: 工具执行前检查参数，命中危险模式则拦截。
PostToolUse: 工具执行后处理输出（如密钥泄露扫描）。

设计理由：用户输入安全 ≠ 工具调用安全。Guardian 只查最初的用户输入，
但 agent 后续生成的具体命令（如 rm -rf）需要在工具执行前再设一道卡。
这是 harness 的纵深防御思想。
"""
from __future__ import annotations
import re

# 危险命令/代码模式（PreToolUse 拦截用）
_DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+-rf\s+/"),            # rm -rf / 删根
    re.compile(r"\brm\s+-rf\s+~"),            # rm -rf ~ 删家目录
    re.compile(r":\(\)\{.*\|.*&\s*\}"),       # fork 炸弹
    re.compile(r"\bmkfs\b"),                  # 格式化磁盘
    re.compile(r"\bdd\s+if=.*of=/dev/"),      # dd 写设备
    re.compile(r">\s*/dev/sd[a-z]"),          # 写裸盘
    re.compile(r"\bshutdown\b|\breboot\b"),   # 关机重启
    re.compile(r"\bchmod\s+-R\s+777\s+/"),    # 全盘改权限
]

# 密钥泄露模式（PostToolUse 扫描用）
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                    # OpenAI 风格 key
    re.compile(r"\b[A-Za-z0-9]{32}\.[A-Za-z0-9]{16,}"),   # 智谱风格 key
    re.compile(r"(?i)(api[_-]?key|secret|password)\s*[=:]\s*\S{8,}"),  # 通用 key=xxx
]


class HookBlocked(Exception):
    """PreToolUse 拦截时抛出，阻止工具执行。"""
    pass


def pre_tool_use(tool_name: str, tool_input: str) -> None:
    """工具执行前检查。命中危险模式抛 HookBlocked 阻止执行。"""
    for pat in _DANGEROUS_PATTERNS:
        if pat.search(tool_input):
            raise HookBlocked(
                f"[PreToolUse 拦截] 工具 {tool_name} 的输入命中危险模式，已阻止执行。"
            )
    # 通过检查，无返回（放行）


def post_tool_use(tool_name: str, output: str) -> str:
    """工具执行后处理。扫描密钥泄露并打码。"""
    redacted = output
    for pat in _SECRET_PATTERNS:
        redacted = pat.sub("[REDACTED_SECRET]", redacted)
    return redacted