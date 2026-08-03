"""CI 验收网关：把 Claude Code 包在完成防线后面，只有防线通过才允许交付。

Claude Code 负责改代码，Aegis 负责判定"能不能算完成"：
CLI 宣称完成后先跑项目自己的检查命令（pre_done 传感器），
不达标就带着失败输出用同一 session `--resume` 打回，直到通过或返工次数用尽。
进程退出码给 CI 用：0 = 通过防线，1 = 拦下，不允许合入。

用法：
    python examples/ci_gate.py --mock
        用脚本化的假 CLI 演示完整链路，不需要任何 API key。

    python examples/ci_gate.py --workspace /path/to/repo \\
        --goal "修掉 days_between 的符号 bug" --check "python3 -m pytest -q"
        用真实 Claude Code CLI。模型由 Claude Code 自己的环境变量决定
        （ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL）。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from harness import (
    ApprovalGate, AuditLog, Budget, ClaudeCodeRuntime, CommandSensor,
    CompletionDefense, FileGuard, Handoff, Policy, SensorBank, ToolContext,
)

DEMO_GOAL = (
    "dateutil_mini.py 里的 days_between 还没有实现。请实现它："
    "返回两个 YYYY-MM-DD 日期之间的天数（b - a），同一天返回 0，b 早于 a 时返回负数。"
    "不要修改函数签名，也不要修改测试文件。"
)
DEMO_CHECK = "python3 -m unittest discover -q"

DEMO_FILES = {
    "dateutil_mini.py": (
        "from datetime import date\n"
        "\n"
        "\n"
        "def days_between(a: str, b: str) -> int:\n"
        '    """返回两个 YYYY-MM-DD 日期之间的天数（b - a），可为负。"""\n'
        "    raise NotImplementedError\n"
    ),
    "test_dateutil.py": (
        "import unittest\n"
        "\n"
        "from dateutil_mini import days_between\n"
        "\n"
        "\n"
        "class DaysBetweenTests(unittest.TestCase):\n"
        "    def test_same_day(self):\n"
        "        self.assertEqual(days_between('2024-01-01', '2024-01-01'), 0)\n"
        "\n"
        "    def test_forward(self):\n"
        "        self.assertEqual(days_between('2024-01-01', '2024-01-02'), 1)\n"
        "\n"
        "    def test_backward_is_negative(self):\n"
        "        self.assertEqual(days_between('2024-01-02', '2024-01-01'), -1)\n"
        "\n"
        "    def test_leap_year(self):\n"
        "        self.assertEqual(days_between('2024-02-28', '2024-03-01'), 2)\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n"
    ),
}


def build_demo_workspace() -> str:
    workspace = tempfile.mkdtemp(prefix="aegis-ci-gate-")
    for name, content in DEMO_FILES.items():
        with open(os.path.join(workspace, name), "w", encoding="utf-8") as handle:
            handle.write(content)
    return workspace


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="", help="被改动的代码库路径")
    parser.add_argument("--goal", default="", help="交给 Claude Code 的任务目标")
    parser.add_argument("--check", default="", help="判定完成的检查命令（项目自己的测试）")
    parser.add_argument("--retries", type=int, default=2, help="防线打回上限")
    parser.add_argument("--timeout", type=int, default=600, help="单次 CLI 调用超时（秒）")
    parser.add_argument("--mock", action="store_true",
                        help="用脚本化的假 CLI 跑通链路，不调真实模型")
    args = parser.parse_args()

    if args.mock:
        workspace = args.workspace or build_demo_workspace()
        goal = args.goal or DEMO_GOAL
        check = args.check or DEMO_CHECK
        command = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "fake_claude_cli.py")]
    else:
        if not args.workspace or not args.goal or not args.check:
            print("真实模式需要 --workspace、--goal 和 --check；"
                  "或先用 --mock 跑通链路。")
            return 2
        workspace = args.workspace
        goal, check, command = args.goal, args.check, "claude"

    workspace = os.path.abspath(workspace)
    state = os.path.join(workspace, ".harness")
    ctx = ToolContext(
        policy=Policy(workspace_root=workspace),
        approval=ApprovalGate(interactive=False),
        audit=AuditLog(os.path.join(state, "audit.jsonl")),
        guard=FileGuard(),
        workspace=workspace,
    )

    # 网关的判据是项目自己的检查命令：确定性、不需要额外模型额度
    sensors = SensorBank()
    sensors.add(CommandSensor("ci-check", check, tier="pre_done", cwd=workspace,
                              timeout=args.timeout,
                              hint="按失败输出修复实现后再宣称完成。"))
    # checklist 留空：Claude Code 用的是它自己的工具，无法标记 Aegis 的清单条目
    defense = CompletionDefense(sensors=sensors, checklist=None, tool_ctx=ctx)
    handoff = Handoff(os.path.join(state, "HANDOFF.md"))

    runtime = ClaudeCodeRuntime(
        workspace=workspace,
        defense=defense,
        budget=Budget(completion_retries=args.retries),
        command=command,
        timeout=args.timeout,
        handoff=handoff,
    )

    print(f"工作区: {workspace}")
    print(f"判据  : {check}")
    print(f"目标  : {goal}\n")

    result = runtime.run(goal)

    print(f"\n状态      : {result['status']}")
    print(f"模型轮次  : {result['turns']}")
    print(f"防线打回  : {result['rejections']}")
    passed = result["status"] == "done"
    print("网关结论  : " + ("通过，允许交付" if passed else "拦下，不允许交付"))
    if not passed:
        print(f"交接记录  : {handoff.path}")
        print((result.get("summary") or "")[:800])
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
