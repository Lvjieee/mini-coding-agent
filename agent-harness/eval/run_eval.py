"""完成防线 A/B 对比实验：同一批任务，开/关完成防线各跑一遍，量化收益。

两个实验臂：
- baseline：关闭完成防线（enable_defense=False）——模型一停止调用工具就接受"完成"，
  无验收清单、无 pre_done 传感器、无独立 Evaluator（模拟朴素 Agent 循环）；
- defense ：完整 harness——Planner 生成验收清单 + pre_done 传感器 + 独立验收 + 打回重注入。

裁判是对 Agent 完全不可见的隐藏测试（运行结束后才写入工作区执行），指标：
- verified_pass：隐藏测试通过（真完成）
- false_done  ：宣称完成但隐藏测试失败（烂尾进入交付，越低越好）
- blocked     ：防线拦下未达标产出、以交接文件收尾（烂尾被拦截，未污染交付）
- rejections  ：完成防线打回次数（返工次数）

用法：
    export OPENAI_BASE_URL=... OPENAI_API_KEY=... HARNESS_MODEL=...
    python eval/run_eval.py                  # 真实模型跑全部任务
    python eval/run_eval.py --runs 3         # 每任务重复 3 次，看方差
    python eval/run_eval.py --mock           # 无需模型，用脚本化模型自检实验管线
                                             #（mock 数字仅验证机制，不可写入简历）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from harness import (
    AgentLoop, ApprovalGate, AuditLog, Budget, Checklist, CommandSensor,
    ContextBuilder, FileGuard, Handoff, Message, OpenAICompatClient, Policy,
    SensorBank, ToolCall, ToolContext, ToolRegistry,
    parse_planner_output, register_builtin, run_subagent,
)

# ---------------------------------------------------------------- 任务集
# 每个任务：starter 文件 + 目标 + 可见检查（defense 臂的 pre_done 传感器）
# + 隐藏测试（裁判，Agent 不可见，覆盖边界情况）。

TASKS = [
    {
        "name": "median",
        "files": {"stats.py": (
            'def median(nums):\n'
            '    """返回列表的中位数。空列表应抛出 ValueError。"""\n'
            '    raise NotImplementedError\n'
        )},
        "goal": ("实现 stats.py 中的 median(nums)：奇数个元素返回中间值；"
                 "偶数个元素返回中间两数的平均值；空列表抛出 ValueError。不要修改函数签名。"),
        "visible_check": 'python3 -c "from stats import median; assert median([1,3,2])==2"',
        "hidden_test": (
            "from stats import median\n"
            "assert median([5]) == 5\n"
            "assert median([1, 3, 2]) == 2\n"
            "assert median([1, 2, 3, 4]) == 2.5\n"
            "assert median([-3, -1, -2]) == -2\n"
            "try:\n"
            "    median([])\n"
            "    raise SystemExit('empty list should raise ValueError')\n"
            "except ValueError:\n"
            "    pass\n"
            "print('OK')\n"
        ),
    },
    {
        "name": "slugify",
        "files": {"text_utils.py": (
            'def slugify(title):\n'
            '    """把标题转成 URL slug。"""\n'
            '    raise NotImplementedError\n'
        )},
        "goal": ("实现 text_utils.py 中的 slugify(title)：转小写；连续空白折叠为单个 '-'；"
                 "移除除字母、数字、'-' 以外的字符；合并连续 '-'；去掉首尾 '-'。"),
        "visible_check": 'python3 -c "from text_utils import slugify; assert slugify(\'Hello World\')==\'hello-world\'"',
        "hidden_test": (
            "from text_utils import slugify\n"
            "assert slugify('Hello World') == 'hello-world'\n"
            "assert slugify('  Hello,  World! ') == 'hello-world'\n"
            "assert slugify('A--B') == 'a-b'\n"
            "assert slugify('Python 3.12 Rocks') == 'python-312-rocks'\n"
            "assert slugify('') == ''\n"
            "print('OK')\n"
        ),
    },
    {
        "name": "parse_csv_line",
        "files": {"csvmini.py": (
            'def parse_csv_line(line):\n'
            '    """解析一行 CSV，返回字段列表。"""\n'
            '    raise NotImplementedError\n'
        )},
        "goal": ("实现 csvmini.py 中的 parse_csv_line(line)：按逗号分割字段；"
                 "支持双引号包裹的字段内含逗号；字段内连续两个双引号 \"\" 表示一个字面双引号。"
                 "返回字符串列表。"),
        "visible_check": "python3 -c \"from csvmini import parse_csv_line; assert parse_csv_line('a,b')==['a','b']\"",
        "hidden_test": (
            "from csvmini import parse_csv_line\n"
            "assert parse_csv_line('a,b,c') == ['a', 'b', 'c']\n"
            "assert parse_csv_line('a,\"b,c\",d') == ['a', 'b,c', 'd']\n"
            'assert parse_csv_line(\'x,"he said ""hi""",y\') == [\'x\', \'he said "hi"\', \'y\']\n'
            "assert parse_csv_line('') == ['']\n"
            "print('OK')\n"
        ),
    },
    {
        "name": "rle",
        "files": {"rle.py": (
            'def encode(s):\n'
            '    """行程编码：\'aab\' -> [(\'a\',2),(\'b\',1)]。"""\n'
            '    raise NotImplementedError\n'
            '\n'
            'def decode(pairs):\n'
            '    """行程解码，encode 的逆操作。"""\n'
            '    raise NotImplementedError\n'
        )},
        "goal": ("实现 rle.py 中的 encode/decode：encode 把字符串转成 (字符, 连续次数) 元组列表，"
                 "decode 还原字符串；空字符串 encode 返回空列表。要求 decode(encode(s)) == s。"),
        "visible_check": "python3 -c \"from rle import encode; assert encode('aab')==[('a',2),('b',1)]\"",
        "hidden_test": (
            "from rle import encode, decode\n"
            "assert encode('') == []\n"
            "assert encode('abc') == [('a',1),('b',1),('c',1)]\n"
            "for s in ['', 'a', 'aaabbbcccd', 'xyzzy', 'aabbaa']:\n"
            "    assert decode(encode(s)) == s, s\n"
            "print('OK')\n"
        ),
    },
    {
        "name": "days_between_bugfix",
        "files": {"dateutil_mini.py": (
            'from datetime import date\n'
            '\n'
            'def days_between(a: str, b: str) -> int:\n'
            '    """返回两个 YYYY-MM-DD 日期之间的天数（b - a），可为负。"""\n'
            '    da = date.fromisoformat(a)\n'
            '    db = date.fromisoformat(b)\n'
            '    return (db - da).days + 1\n'
        )},
        "goal": ("dateutil_mini.py 的 days_between 有 bug：同一天应返回 0，"
                 "days_between('2024-01-01','2024-01-02') 应返回 1。修复它，不要修改函数签名。"),
        "visible_check": "python3 -c \"from dateutil_mini import days_between; assert days_between('2024-01-01','2024-01-02')==1\"",
        "hidden_test": (
            "from dateutil_mini import days_between\n"
            "assert days_between('2024-01-01', '2024-01-01') == 0\n"
            "assert days_between('2024-01-01', '2024-01-02') == 1\n"
            "assert days_between('2024-01-02', '2024-01-01') == -1\n"
            "assert days_between('2024-02-28', '2024-03-01') == 2  # 闰年\n"
            "print('OK')\n"
        ),
    },
    # ------------------------------------------------ 以下为高难度陷阱任务
    # 规格里埋了多个容易被忽略的要求，隐藏测试逐条核查，用于区分两个臂。
    {
        "name": "merge_intervals",
        "files": {"intervals.py": (
            'def merge_intervals(intervals):\n'
            '    """合并区间，返回 (start, end) 元组列表。"""\n'
            '    raise NotImplementedError\n'
        )},
        "goal": ("实现 intervals.py 中的 merge_intervals(intervals)："
                 "输入是整数二元组 (start, end) 列表，可能无序；"
                 "重叠或首尾相接的区间都要合并（如 (1,2) 与 (2,3) 合并为 (1,3)）；"
                 "返回按 start 升序排列的元组列表；空输入返回 []。不要修改函数签名。"),
        "visible_check": "python3 -c \"from intervals import merge_intervals; assert merge_intervals([(1,3),(2,4)])==[(1,4)]\"",
        "hidden_test": (
            "from intervals import merge_intervals\n"
            "assert merge_intervals([]) == []\n"
            "assert merge_intervals([(5,6)]) == [(5,6)]\n"
            "assert merge_intervals([(1,2),(2,3)]) == [(1,3)]  # 首尾相接也要合并\n"
            "assert merge_intervals([(4,5),(1,2)]) == [(1,2),(4,5)]  # 无序输入\n"
            "assert merge_intervals([(1,10),(2,3),(4,5)]) == [(1,10)]  # 嵌套\n"
            "assert merge_intervals([(-3,-1),(-2,0),(2,3)]) == [(-3,0),(2,3)]\n"
            "print('OK')\n"
        ),
    },
    {
        "name": "format_size",
        "files": {"humanize_mini.py": (
            'def format_size(n):\n'
            '    """把字节数格式化成人类可读字符串。"""\n'
            '    raise NotImplementedError\n'
        )},
        "goal": ("实现 humanize_mini.py 中的 format_size(n)：以 1024 为进制，"
                 "单位依次为 B/KB/MB/GB/TB，选取使数值 >= 1 的最大单位；"
                 "数值保留 1 位小数，但结尾的 '.0' 要去掉（1024 -> '1KB' 而不是 '1.0KB'）；"
                 "0 返回 '0B'；小于 1024 的整数直接用 B（512 -> '512B'）；"
                 "n 为负数时抛出 ValueError。不要修改函数签名。"),
        "visible_check": "python3 -c \"from humanize_mini import format_size; assert format_size(1536)=='1.5KB'\"",
        "hidden_test": (
            "from humanize_mini import format_size\n"
            "assert format_size(0) == '0B'\n"
            "assert format_size(512) == '512B'\n"
            "assert format_size(1023) == '1023B'\n"
            "assert format_size(1024) == '1KB'  # 去掉 .0\n"
            "assert format_size(1536) == '1.5KB'\n"
            "assert format_size(3 * 1024**3) == '3GB'\n"
            "assert format_size(10 * 1024**4) == '10TB'\n"
            "try:\n"
            "    format_size(-1)\n"
            "    raise SystemExit('negative should raise ValueError')\n"
            "except ValueError:\n"
            "    pass\n"
            "print('OK')\n"
        ),
    },
    {
        "name": "cart_total",
        "files": {
            "pricing.py": (
                'def apply_discount(subtotal, rate):\n'
                '    """rate 是 0~1 的折扣率，返回折后金额。"""\n'
                '    return subtotal * rate\n'
                '\n'
                'TAX = 0.1\n'
                '\n'
                'def with_tax(amount):\n'
                '    return round(amount * (1 + TAX), 2)\n'
            ),
            "cart.py": (
                'from pricing import apply_discount, with_tax\n'
                '\n'
                'def cart_total(items, discount_rate=0.0):\n'
                '    """items: [(price, qty)] 列表。先合计，再打折，最后加税，保留两位小数。"""\n'
                '    subtotal = 0\n'
                '    for price, qty in items:\n'
                '        subtotal += price\n'
                '    return with_tax(apply_discount(subtotal, discount_rate))\n'
            ),
        },
        "goal": ("购物车模块有 bug：cart_total([(10.0, 2), (5.0, 1)], 0.1) 应返回 24.75，"
                 "现在算出来不对。排查 cart.py 和 pricing.py，修复所有相关 bug，"
                 "保持「先合计、再打折、最后加税、保留两位小数」的语义，不要修改任何函数签名。"),
        "visible_check": "python3 -c \"from cart import cart_total; assert cart_total([(10.0,2),(5.0,1)], 0.1)==24.75\"",
        "hidden_test": (
            "from cart import cart_total\n"
            "assert cart_total([(10.0, 2), (5.0, 1)], 0.1) == 24.75\n"
            "assert cart_total([(3.0, 4)]) == 13.2  # 无折扣\n"
            "assert cart_total([(10.0, 0)]) == 0.0  # 数量为 0\n"
            "assert cart_total([(1.0, 1), (2.0, 2)], 0.5) == 2.75\n"
            "assert cart_total([], 0.3) == 0.0\n"
            "print('OK')\n"
        ),
    },
    {
        "name": "parse_duration",
        "files": {"timeparse.py": (
            'def parse_duration(s):\n'
            '    """把 \'1h30m15s\' 这类时长字符串解析成总秒数。"""\n'
            '    raise NotImplementedError\n'
        )},
        "goal": ("实现 timeparse.py 中的 parse_duration(s)：支持 h/m/s 三种组件，"
                 "每个组件是非负整数加单位（如 '1h30m15s' -> 5415）；组件可以缺省但必须"
                 "按 h、m、s 的顺序出现（'90m' 合法 -> 5400，'1m1h' 非法）；"
                 "至少要有一个组件；空串、乱序、缺数字（'h'）、小数（'1.5h'）、"
                 "含空格或其他字符的输入一律抛出 ValueError。不要修改函数签名。"),
        "visible_check": "python3 -c \"from timeparse import parse_duration; assert parse_duration('90m')==5400\"",
        "hidden_test": (
            "from timeparse import parse_duration\n"
            "assert parse_duration('1h30m15s') == 5415\n"
            "assert parse_duration('90m') == 5400\n"
            "assert parse_duration('2h') == 7200\n"
            "assert parse_duration('45s') == 45\n"
            "assert parse_duration('1h5s') == 3605\n"
            "for bad in ['', '1m1h', 'h', '1.5h', ' 1h', '1h ', '1x', '1h30']:\n"
            "    try:\n"
            "        parse_duration(bad)\n"
            "        raise SystemExit('should raise ValueError: %r' % bad)\n"
            "    except ValueError:\n"
            "        pass\n"
            "print('OK')\n"
        ),
    },
]


# ---------------------------------------------------------------- mock 模型
# 仅用于自检实验管线：先交一版有 bug 的实现并宣称完成；被防线打回后再交正确版。
# baseline 臂会把第一版当成"完成"（false_done），defense 臂会打回并得到修复版。

MOCK_SOLUTIONS = {
    "median": {
        "path": "stats.py",
        "buggy": (
            'def median(nums):\n'
            '    """返回列表的中位数。空列表应抛出 ValueError。"""\n'
            '    return nums[len(nums) // 2]  # 未排序、未处理偶数与空列表\n'
        ),
        "fixed": (
            'def median(nums):\n'
            '    """返回列表的中位数。空列表应抛出 ValueError。"""\n'
            '    if not nums:\n'
            '        raise ValueError("empty list")\n'
            '    s = sorted(nums)\n'
            '    n = len(s)\n'
            '    mid = n // 2\n'
            '    if n % 2:\n'
            '        return s[mid]\n'
            '    return (s[mid - 1] + s[mid]) / 2\n'
        ),
    },
}


class MockClient:
    """按脚本回放的假模型：read → 交 buggy 版 → 宣称完成 → (被打回) → 交 fixed 版 → 宣称完成。"""

    def __init__(self, path: str, buggy: str, fixed: str):
        self.script = [
            Message("assistant", tool_calls=[ToolCall("m1", "read_file", {"path": path})]),
            Message("assistant", tool_calls=[ToolCall("m2", "write_file", {"path": path, "content": buggy})]),
            Message("assistant", content="任务已完成。"),
            Message("assistant", tool_calls=[ToolCall("m3", "write_file", {"path": path, "content": fixed})]),
            Message("assistant", content="已修复问题，任务完成。"),
        ]

    def complete(self, messages, tools):
        if self.script:
            return self.script.pop(0)
        return Message("assistant", content="任务已完成。")


class MockEvaluator:
    def complete(self, messages, tools):
        return Message("assistant", content='{"verdict": "accept", "issues": []}')


# ---------------------------------------------------------------- 实验执行

def build_loop(workspace: str, arm: str, client, evaluator, task: dict) -> tuple[AgentLoop, Checklist]:
    state = os.path.join(workspace, ".harness")
    ctx = ToolContext(
        policy=Policy(workspace_root=workspace),
        approval=ApprovalGate(interactive=False),
        audit=AuditLog(os.path.join(state, "audit.jsonl")),
        guard=FileGuard(),
        workspace=workspace,
    )
    defense = arm == "defense"
    checklist = Checklist(os.path.join(state, "checklist.json"))
    handoff = Handoff(os.path.join(state, "HANDOFF.md"))
    registry = ToolRegistry(ctx)
    register_builtin(registry, checklist=checklist if defense else None, handoff=handoff)
    sensors = SensorBank()
    if defense and task.get("visible_check"):
        sensors.add(CommandSensor(
            "visible-check", task["visible_check"], tier="pre_done",
            cwd=workspace, timeout=60,
            hint="按报错修复实现后再宣称完成。"))
    loop = AgentLoop(
        client=client,
        context=ContextBuilder(workspace),
        registry=registry,
        sensors=sensors,
        checklist=checklist,
        handoff=handoff,
        evaluator_client=evaluator if defense else None,
        budget=Budget(max_turns=25, max_tool_calls=80, completion_retries=3),
        enable_defense=defense,
    )
    return loop, checklist


def run_hidden_test(workspace: str, task: dict) -> tuple[bool, str]:
    """运行结束后才把隐藏测试写入工作区执行——Agent 全程不可见。"""
    path = os.path.join(workspace, "_hidden_test.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(task["hidden_test"])
    proc = subprocess.run(
        [sys.executable, "_hidden_test.py"], cwd=workspace,
        capture_output=True, text=True, timeout=60)
    detail = (proc.stdout + proc.stderr).strip()[-300:]
    return proc.returncode == 0, detail


def run_one(root: str, task: dict, arm: str, run_idx: int, mock: bool,
            client_factory, evaluator) -> dict:
    workspace = os.path.join(root, f"{task['name']}-{arm}-{run_idx}")
    os.makedirs(workspace, exist_ok=True)
    for rel, content in task["files"].items():
        with open(os.path.join(workspace, rel), "w", encoding="utf-8") as f:
            f.write(content)

    client = client_factory(task)
    loop, checklist = build_loop(workspace, arm, client, evaluator, task)

    if arm == "defense" and not mock:
        plan = run_subagent(client, "planner", f"工作区：{workspace}\n目标：{task['goal']}")
        behaviors = parse_planner_output(plan) or [f"目标「{task['goal'][:60]}」已实现且有可核查证据。"]
        checklist.bulk_add(behaviors)

    started = time.time()
    result = loop.run(task["goal"])
    verified, detail = run_hidden_test(workspace, task)

    claimed_done = result["status"] == "done"
    return {
        "task": task["name"], "arm": arm, "run": run_idx,
        "status": result["status"],
        "claimed_done": claimed_done,
        "verified_pass": verified,
        "false_done": claimed_done and not verified,
        "blocked": (not claimed_done),
        "rejections": result.get("rejections", 0),
        "turns": result.get("turns", 0),
        "tool_calls": result.get("tool_calls", 0),
        "seconds": round(time.time() - started, 1),
        "hidden_detail": "" if verified else detail,
    }


def aggregate(rows: list[dict]) -> dict:
    out = {}
    for arm in ("baseline", "defense"):
        sub = [r for r in rows if r["arm"] == arm]
        done = [r for r in sub if r.get("status") != "error"]  # 平均值只统计成功完成的运行
        n = len(done) or 1
        out[arm] = {
            "runs": len(sub),
            "errors": len(sub) - len(done),
            "claimed_done": sum(r["claimed_done"] for r in done),
            "verified_pass": sum(r["verified_pass"] for r in done),
            "false_done": sum(r["false_done"] for r in done),
            "blocked": sum(r["blocked"] for r in done),
            "avg_rejections": round(sum(r["rejections"] for r in done) / n, 2),
            "avg_turns": round(sum(r["turns"] for r in done) / n, 1),
        }
    return out


def print_report(rows: list[dict], summary: dict, mock: bool):
    print("\n===== 单次运行明细 =====")
    for r in rows:
        if r.get("status") == "error":
            flag = "运行出错"
        elif r["verified_pass"] and r["claimed_done"]:
            flag = "真完成"
        elif r["false_done"]:
            flag = "烂尾交付!"
        elif r["blocked"]:
            flag = "防线拦截"
        else:
            flag = "未通过"
        print(f"  {r['task']:<22} {r['arm']:<9} run{r['run']}  "
              f"status={r['status']:<28} 隐藏测试={'过' if r['verified_pass'] else '挂'}  "
              f"返工={r['rejections']}  [{flag}]")

    print("\n===== 汇总 =====")
    header = f"{'指标':<26}{'baseline(关防线)':<20}{'defense(开防线)':<20}"
    print(header)
    keys = [("runs", "运行次数"), ("errors", "运行出错(未计入)"),
            ("claimed_done", "宣称完成"),
            ("verified_pass", "隐藏测试通过(真完成)"), ("false_done", "烂尾进入交付"),
            ("blocked", "防线拦截/未交付"), ("avg_rejections", "平均返工次数"),
            ("avg_turns", "平均轮次")]
    for key, label in keys:
        print(f"{label:<26}{str(summary['baseline'][key]):<20}{str(summary['defense'][key]):<20}")
    if mock:
        print("\n[注意] 本次为 --mock 管线自检，模型行为是脚本化的，数字只证明机制有效，不可用于简历。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1, help="每个任务每个臂重复次数")
    parser.add_argument("--tasks", type=str, default="", help="逗号分隔的任务名子集")
    parser.add_argument("--mock", action="store_true", help="脚本化模型自检管线（不调真实模型）")
    args = parser.parse_args()

    tasks = TASKS
    if args.mock:
        tasks = [t for t in TASKS if t["name"] in MOCK_SOLUTIONS]
    if args.tasks:
        wanted = set(args.tasks.split(","))
        tasks = [t for t in tasks if t["name"] in wanted]

    if args.mock:
        def client_factory(task):
            sol = MOCK_SOLUTIONS[task["name"]]
            return MockClient(sol["path"], sol["buggy"], sol["fixed"])
        evaluator = MockEvaluator()
    else:
        if not os.environ.get("OPENAI_API_KEY"):
            print("缺少 OPENAI_API_KEY。真实模型评测需要配置：\n"
                  "  export OPENAI_BASE_URL=... OPENAI_API_KEY=... HARNESS_MODEL=...\n"
                  "或先用 --mock 自检实验管线。")
            sys.exit(1)
        model = os.environ.get("HARNESS_MODEL", "gpt-5.2")
        eval_model = os.environ.get("HARNESS_EVAL_MODEL")

        def client_factory(task):
            return OpenAICompatClient(model=model)
        evaluator = OpenAICompatClient(model=eval_model) if eval_model else None

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "runs", time.strftime("%Y%m%d-%H%M%S"))
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)

    rows = []
    for task in tasks:
        for arm in ("baseline", "defense"):
            for i in range(args.runs):
                print(f"运行 {task['name']} / {arm} / #{i} ...")
                try:
                    rows.append(run_one(root, task, arm, i, args.mock, client_factory, evaluator))
                except Exception as e:  # 单次运行失败（如网关超时）不应丢弃整批已完成结果
                    print(f"  [跳过] {task['name']}/{arm}/#{i} 失败：{type(e).__name__}: {e}")
                    rows.append({
                        "task": task["name"], "arm": arm, "run": i, "status": "error",
                        "claimed_done": False, "verified_pass": False, "false_done": False,
                        "blocked": False, "error": f"{type(e).__name__}: {e}",
                        "rejections": 0, "turns": 0, "tool_calls": 0, "seconds": 0,
                        "hidden_detail": "",
                    })

    summary = aggregate(rows)
    with open(os.path.join(root, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "summary": summary, "mock": args.mock}, f,
                  ensure_ascii=False, indent=1)
    print_report(rows, summary, args.mock)
    print(f"\n结果已保存: {os.path.join(root, 'results.json')}")


if __name__ == "__main__":
    main()
