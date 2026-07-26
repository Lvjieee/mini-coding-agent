<file>
     1→"""示例入口：把 harness 各层装配起来执行一个任务。
     2→
     3→用法：
     4→    export OPENAI_BASE_URL=...   # 任何 OpenAI 兼容接口
     5→    export OPENAI_API_KEY=...
     6→    export HARNESS_MODEL=...     # 主模型（Generator）
     7→    export HARNESS_EVAL_MODEL=.. # 可选：验收用不同模型，降低"实现与验收共享同一误解"的风险
     8→    python examples/run_task.py "你的任务目标" [工作区路径]
     9→"""
    10→from __future__ import annotations
    11→
    12→import os
    13→import sys
    14→
    15→sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    16→
    17→from harness import (
    18→    AgentLoop, ApprovalGate, AuditLog, Budget, Checklist, CommandSensor,
    19→    ContextBuilder, FileGuard, Handoff, MemoryStore, OpenAICompatClient,
    20→    Policy, SensorBank, SkillLibrary, ToolContext, ToolRegistry,
    21→    parse_planner_output, register_builtin, run_subagent,
    22→)
    23→
    24→
    25→def main():
    26→    if len(sys.argv) < 2:
    27→        print(__doc__)
    28→        sys.exit(1)
    29→    goal = sys.argv[1]
    30→    workspace = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else ".")
    31→    state_dir = os.path.join(workspace, ".harness")
    32→
    33→    # 模型：无状态函数；主模型与验收模型可以不同
    34→    client = OpenAICompatClient(model=os.environ.get("HARNESS_MODEL", "gpt-5.2"))
    35→    eval_model = os.environ.get("HARNESS_EVAL_MODEL")
    36→    evaluator = OpenAICompatClient(model=eval_model) if eval_model else None
    37→
    38→    # 运行环境层：权限边界 + 审批门 + 审计 + 写入保护
    39→    policy = Policy(workspace_root=workspace)
    40→    ctx = ToolContext(
    41→        policy=policy,
    42→        approval=ApprovalGate(),  # 非交互环境下高危操作默认拒绝
    43→        audit=AuditLog(os.path.join(state_dir, "audit.jsonl")),
    44→        guard=FileGuard(),
    45→        workspace=workspace,
    46→    )
    47→
    48→    # 状态承载：验收清单、交接、记忆、Skill
    49→    checklist = Checklist(os.path.join(state_dir, "checklist.json"))
    50→    handoff = Handoff(os.path.join(state_dir, "HANDOFF.md"))
    51→    memory = MemoryStore(os.path.join(state_dir, "memory.json"))
    52→    skills = SkillLibrary(os.path.join(workspace, "skills"))
    53→
    54→    # 工具层：内置工具 + 渐进式加载（扩展工具/MCP 封装可注册为 core=False）
    55→    registry = ToolRegistry(ctx)
    56→    register_builtin(registry, checklist=checklist, handoff=handoff,
    57→                     memory=memory, skills=skills)
    58→
    59→    # 反馈层：计算型传感器，按工程实际接入（示例：Python 语法检查）
    60→    sensors = SensorBank()
    61→    sensors.add(CommandSensor(
    62→        "py-compile", "python -m compileall -q .", tier="post_edit",
    63→        cwd=workspace, hint="按报错位置修复语法问题后重试。"))
    64→    # sensors.add(CommandSensor("tests", "pytest -q", tier="pre_done", cwd=workspace))
    65→    # sensors.add(CommandSensor("lint", "ruff check .", tier="post_edit", cwd=workspace))
    66→
    67→    # Planner：新任务先展开成行为级验收清单（续跑时沿用已有清单）
    68→    if not checklist.items:
    69→        plan = run_subagent(client, "planner", f"工作区：{workspace}\n目标：{goal}")
    70→        behaviors = parse_planner_output(plan)
    71→        if not behaviors:
    72→            behaviors = [f"目标「{goal}」已实现，且有可核查的运行证据。"]
    73→        checklist.bulk_add(behaviors)
    74→        print("验收清单：\n" + checklist.render())
    75→
    76→    loop = AgentLoop(
    77→        client=client,
    78→        context=ContextBuilder(workspace),
    79→        registry=registry,
    80→        sensors=sensors,
    81→        checklist=checklist,
    82→        handoff=handoff,
    83→        memory=memory,
    84→        skills=skills,
    85→        evaluator_client=evaluator,
    86→        budget=Budget(),
    87→    )
    88→    result = loop.run(goal)
    89→    print(f"\n状态: {result['status']}")
    90→    print(result.get("summary", ""))
    91→    if result["status"] != "done":
    92→        print(f"交接记录: {handoff.path}")
    93→
    94→
    95→if __name__ == "__main__":
    96→    main()
    97→
</file>
<metadata>The file has 97 lines in total.</metadata>