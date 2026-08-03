"""示例入口：把 harness 各层装配起来执行一个任务。

用法：
    export OPENAI_BASE_URL=...   # 任何 OpenAI 兼容接口
    export OPENAI_API_KEY=...
    export HARNESS_MODEL=...     # 主模型（Generator）
    export HARNESS_EVAL_MODEL=.. # 可选：验收用不同模型，降低"实现与验收共享同一误解"的风险
    python examples/run_task.py "你的任务目标" [工作区路径]
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from harness import (
    AgentLoop, ApprovalGate, AuditLog, Budget, Checklist, CommandSensor,
    ContextBuilder, FileGuard, Handoff, MemoryStore, OpenAICompatClient,
    Policy, SensorBank, SkillLibrary, ToolContext, ToolRegistry,
    parse_planner_output, register_builtin, run_subagent,
)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    goal = sys.argv[1]
    workspace = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")
    state_dir = os.path.join(workspace, ".harness")

    # 模型：无状态函数；主模型与验收模型可以不同
    client = OpenAICompatClient(model=os.environ.get("HARNESS_MODEL", "gpt-5.2"))
    eval_model = os.environ.get("HARNESS_EVAL_MODEL")
    evaluator = OpenAICompatClient(model=eval_model) if eval_model else None

    # 运行环境层：权限边界 + 审批门 + 审计 + 写入保护
    policy = Policy(workspace_root=workspace)
    ctx = ToolContext(
        policy=policy,
        approval=ApprovalGate(),  # 非交互环境下高危操作默认拒绝
        audit=AuditLog(os.path.join(state_dir, "audit.jsonl")),
        guard=FileGuard(),
        workspace=workspace,
    )

    # 状态承载：验收清单、交接、记忆、Skill
    checklist = Checklist(os.path.join(state_dir, "checklist.json"))
    handoff = Handoff(os.path.join(state_dir, "HANDOFF.md"))
    memory = MemoryStore(os.path.join(state_dir, "memory.json"))
    skills = SkillLibrary(os.path.join(workspace, "skills"))

    # 工具层：内置工具 + 渐进式加载（扩展工具/MCP 封装可注册为 core=False）
    registry = ToolRegistry(ctx)
    register_builtin(registry, checklist=checklist, handoff=handoff,
                     memory=memory, skills=skills)

    # 反馈层：计算型传感器，按工程实际接入（示例：Python 语法检查）
    sensors = SensorBank()
    sensors.add(CommandSensor(
        "py-compile", "python -m compileall -q .", tier="post_edit",
        cwd=workspace, hint="按报错位置修复语法问题后重试。"))
    # sensors.add(CommandSensor("tests", "pytest -q", tier="pre_done", cwd=workspace))
    # sensors.add(CommandSensor("lint", "ruff check .", tier="post_edit", cwd=workspace))

    # Planner：新任务先展开成行为级验收清单（续跑时沿用已有清单）
    if not checklist.items:
        plan = run_subagent(client, "planner", f"工作区：{workspace}\n目标：{goal}")
        behaviors = parse_planner_output(plan)
        if not behaviors:
            behaviors = [f"目标「{goal}」已实现，且有可核查的运行证据。"]
        checklist.bulk_add(behaviors)
        print("验收清单：\n" + checklist.render())

    loop = AgentLoop(
        client=client,
        context=ContextBuilder(workspace),
        registry=registry,
        sensors=sensors,
        checklist=checklist,
        handoff=handoff,
        memory=memory,
        skills=skills,
        evaluator_client=evaluator,
        budget=Budget(),
    )
    result = loop.run(goal)
    print(f"\n状态: {result['status']}")
    print(result.get("summary", ""))
    if result["status"] != "done":
        print(f"交接记录: {handoff.path}")


if __name__ == "__main__":
    main()
