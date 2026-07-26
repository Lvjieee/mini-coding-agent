"""隔离（Isolate）与角色分工：Sub-agent 只带自己需要的上下文，只把结果带回主线。

借鉴 Anthropic 的 Planner / Generator / Evaluator 对抗式分工：
- Planner：把一句话需求展开成范围明确的规格与行为级验收清单，不指定实现细节；
- Generator：主循环本身，逐项实现、提交前自检；
- Evaluator：独立验收 Agent，不轻信实现者的结论，逐条核查并引用证据；
  建议用不同的模型实例（实现与验收共享同一误解时，测试全过也不可信）。
"""
from __future__ import annotations

import json
import re

from .model import Message, ModelClient
from .tools import ToolRegistry, render_result

ROLES = {
    "planner": (
        "你是规划 Agent（Planner）。把用户的目标展开成一份行为级验收清单：\n"
        "- 每条是一个可独立验证的具体行为描述（做什么、怎么算通过），不是实现步骤；\n"
        "- 覆盖主要功能、边界情况和可验证的质量要求；定义范围，但不指定实现细节；\n"
        "- 条数与任务规模匹配（小任务 3-8 条，大任务可以更多）。\n"
        "只输出一个 JSON 字符串数组，每个元素一条行为描述，不要输出其他内容。"
    ),
    "evaluator": (
        "你是独立验收 Agent（Evaluator）。对照验收清单核查实际产出：\n"
        "- 不要轻信实现者的说法，用只读工具和命令逐条实际核查，并引用证据（命令输出、文件内容与行号）；\n"
        "- 发现问题要定位到具体文件与位置，说明期望与实际的差异；\n"
        "- 不得降低标准，不得跳过条目。\n"
        "核查完成后输出 JSON：{\"verdict\": \"accept\" 或 \"reject\", \"issues\": [\"问题描述…\"]}。"
    ),
    "researcher": (
        "你是调研 Agent。围绕给定问题收集信息，返回：结论、证据、来源、适用范围。"
        "区分原始资料的观点和你自己的推论。"
    ),
}


def run_subagent(
    client: ModelClient,
    role: str,
    task: str,
    registry: ToolRegistry | None = None,
    max_turns: int = 15,
) -> str:
    """在全新的隔离上下文中运行一个子代理，返回其最终文本结论。"""
    system = Message("system", ROLES[role])
    history: list[Message] = [Message("user", task)]
    tools = registry.schemas() if registry else []
    for _ in range(max_turns):
        msg = client.complete([system] + history, tools)
        history.append(msg)
        if not msg.tool_calls:
            return msg.content
        for tc in msg.tool_calls:
            out = registry.execute(tc.name, tc.arguments)
            history.append(Message("tool", render_result(out), tool_call_id=tc.id))
    return history[-1].content or "(子代理超出轮次预算，未得出结论)"


def parse_planner_output(text: str) -> list[str]:
    """从 Planner 输出中提取 JSON 数组；失败则返回空列表由调用方兜底。"""
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return [str(x) for x in data if str(x).strip()]


def parse_evaluator_output(text: str) -> tuple[bool, list[str]]:
    """返回 (是否通过, 问题列表)。解析失败时按未通过处理（fail-closed）。"""
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            verdict = str(data.get("verdict", "")).lower()
            issues = [str(i) for i in data.get("issues", [])]
            return verdict == "accept", issues
        except json.JSONDecodeError:
            pass
    return False, [f"验收输出无法解析，原文：{text[:500]}"]
