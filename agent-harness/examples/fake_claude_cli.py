"""脚本化的假 Claude Code CLI，用于在没有模型额度时演示 CI 验收网关。

只实现 ClaudeCodeRuntime 依赖的那部分契约：接受 `-p` / `--resume`，
向 stdout 打印一个 JSON 结果对象。行为刻意模拟真实失效模式——
第一次交一版能过"看起来对"的实现但没通过项目测试，并宣称完成；
被完成防线打回（带 --resume）后才交出修好的版本。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

TARGET = "dateutil_mini.py"

BUGGY = (
    "from datetime import date\n"
    "\n"
    "\n"
    "def days_between(a: str, b: str) -> int:\n"
    '    """返回两个 YYYY-MM-DD 日期之间的天数（b - a），可为负。"""\n'
    "    da = date.fromisoformat(a)\n"
    "    db = date.fromisoformat(b)\n"
    "    return abs((db - da).days)\n"
)

FIXED = (
    "from datetime import date\n"
    "\n"
    "\n"
    "def days_between(a: str, b: str) -> int:\n"
    '    """返回两个 YYYY-MM-DD 日期之间的天数（b - a），可为负。"""\n'
    "    da = date.fromisoformat(a)\n"
    "    db = date.fromisoformat(b)\n"
    "    return (db - da).days\n"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", dest="prompt", default="")
    parser.add_argument("--resume", dest="session", default=None)
    parser.add_argument("--permission-mode", dest="permission_mode", default="")
    parser.add_argument("--output-format", dest="output_format", default="json")
    args = parser.parse_args()

    resumed = bool(args.session)
    with open(TARGET, "w", encoding="utf-8") as handle:
        handle.write(FIXED if resumed else BUGGY)

    result = (
        "已按打回信息修正符号方向，days_between 现在返回带符号的天数差。"
        if resumed else
        "已实现 days_between，返回两个日期之间的天数。任务完成。"
    )
    json.dump({
        "is_error": False,
        "subtype": "success",
        "session_id": args.session or "fake-session",
        "num_turns": 2,
        "result": result,
        "permission_denials": [],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "modelUsage": {"fake-model": {"inputTokens": 0, "outputTokens": 0}},
    }, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    os.chdir(os.getcwd())
    sys.exit(main())
