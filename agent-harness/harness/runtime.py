"""Runtime 接口：把"谁来跑 agent 循环"这件事抽象出来。

Harness 的立场是：可靠性来自模型外部的控制系统，而"跑循环"本身可以换实现。
- `AgentLoop`（loop.py）——自带循环，直连 OpenAI 兼容接口；
- `ClaudeCodeRuntime`（claude_runtime.py）——把循环交给 Claude Code CLI，
  harness 退到进程层做编排与验收。

两者都满足下面的 `Runtime` 协议，因此可以在同一套评测里互换，
用来回答"换掉执行引擎后，完成防线的收益还成立吗"。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


#: `Runtime.run()` 返回字典的约定字段。
#:
#: 必填：
#:   status      —— "done" 表示通过完成防线正常交付；其他值为停止原因
#:                  （budget_turns / budget_tool_calls / completion_retries_exhausted / error）
#:   turns       —— 模型交互轮次，防线的代价侧指标
#:   rejections  —— 完成防线打回次数（返工次数）
#:   summary     —— 收尾说明或最后一条模型输出
#:
#: 选填：
#:   tool_calls  —— 工具调用次数（Claude Code 的 headless 输出不一定给得出）
#:   unresolved  —— 未通过的验收清单条目 id
#:   handoff     —— 交接文件路径
RESULT_FIELDS = (
    "status", "turns", "rejections", "summary",
    "tool_calls", "unresolved", "handoff",
)

#: 表示"通过完成防线、正常交付"的 status 取值。
STATUS_DONE = "done"


@runtime_checkable
class Runtime(Protocol):
    """执行引擎协议：给一个目标，跑到交付或停止，返回结构化结果。"""

    def run(self, goal: str) -> dict:
        """执行目标，返回符合 RESULT_FIELDS 约定的字典。

        实现方必须保证：
        - 只有在完成防线全部通过时才返回 status == STATUS_DONE；
        - 预算耗尽或打回次数用尽时，写好交接文件再返回，不硬撑；
        - 即使中途失败也要返回结构化结果，便于批量评测统计，不抛异常穿透。
        """
        ...


def is_delivered(result: dict) -> bool:
    """结果是否算"宣称完成并进入交付"——评测里 false_done 的判定前提。"""
    return result.get("status") == STATUS_DONE
