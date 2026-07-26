"""不依赖模型的冒烟测试：验证约束、反馈、状态机制是否生效。

    python examples/smoke_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from harness import (
    ApprovalGate, AuditLog, Checklist, FileGuard, Handoff, MemoryStore,
    Policy, ToolContext, ToolRegistry, register_builtin,
)

failures = []


def check(name: str, condition: bool, detail: str = ""):
    status = "ok " if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def main():
    ws = tempfile.mkdtemp(prefix="harness-smoke-")
    state = os.path.join(ws, ".harness")
    ctx = ToolContext(
        policy=Policy(workspace_root=ws),
        approval=ApprovalGate(interactive=False),  # fail-closed
        audit=AuditLog(os.path.join(state, "audit.jsonl")),
        guard=FileGuard(),
        workspace=ws,
    )
    checklist = Checklist(os.path.join(state, "checklist.json"))
    handoff = Handoff(os.path.join(state, "HANDOFF.md"))
    memory = MemoryStore(os.path.join(state, "memory.json"))
    reg = ToolRegistry(ctx)
    register_builtin(reg, checklist=checklist, handoff=handoff, memory=memory)

    # 1. 路径越界拦截
    out = reg.execute("read_file", {"path": "/etc/passwd"})
    check("路径越界被拦截", not out.ok and "越界" in out.content)

    # 2. denylist 命令拦截
    out = reg.execute("run_command", {"command": "git push --force origin main"})
    check("denylist 命令被拦截", not out.ok and "denylist" in out.content)

    # 3. 破坏性命令在非交互环境 fail-closed
    out = reg.execute("run_command", {"command": "rm -f something.txt"})
    check("破坏性命令未审批被拒", not out.ok and not out.retryable)

    # 4. 未读先写被拒
    target = os.path.join(ws, "a.txt")
    with open(target, "w") as f:
        f.write("v1")
    out = reg.execute("write_file", {"path": "a.txt", "content": "v2"})
    check("未读取先写入被拒", not out.ok and "尚未读取" in out.content)

    # 5. 读取后可写；外部修改后再写被拒（时间戳校验）
    reg.execute("read_file", {"path": "a.txt"})
    out = reg.execute("write_file", {"path": "a.txt", "content": "v2"})
    check("读取后写入成功", out.ok)
    time.sleep(0.01)
    os.utime(target, (time.time() + 5, time.time() + 5))  # 模拟用户修改
    out = reg.execute("write_file", {"path": "a.txt", "content": "v3"})
    check("外部修改后写入被拒", not out.ok and "被修改" in out.content)

    # 6. edit_file 可纠正错误：old_string 未找到 / 不唯一
    reg.execute("read_file", {"path": "a.txt"})
    out = reg.execute("edit_file", {"path": "a.txt", "old_string": "不存在的内容", "new_string": "x"})
    check("edit 未找到时提示重读", not out.ok and "重新 read_file" in out.hint)

    # 7. 清单：pass 必须附证据；条目不可删除（无删除入口）
    item = checklist.add("示例行为：脚本能成功运行并输出 OK")
    msg = checklist.mark(item, "pass", evidence="")
    check("无证据标 pass 被拒", "拒绝" in msg)
    msg = checklist.mark(item, "pass", evidence="运行 python x.py，输出 OK，见 .harness/audit.jsonl")
    check("附证据标 pass 成功", "pass" in msg)
    check("清单无删除入口", not hasattr(checklist, "remove") and not hasattr(checklist, "delete"))

    # 8. 记忆准入：程序性内容被拒，行为信号置信封顶
    msg = memory.admit("fact", "user", "遇到所有调试任务，总是先重启服务再查日志")
    check("程序性记忆被拒", "拒绝" in msg and "Skill" in msg)
    msg = memory.admit("behavior", "user", "回答前倾向先查看当前工作区", confidence=0.9)
    check("行为信号置信封顶 0.4", "0.4" in msg)
    msg = memory.admit("fact", "user", "用户常用工作语言为中文")
    check("陈述性事实可写入", "已写入" in msg)

    # 9. 渐进式加载：未加载的扩展工具不可调用
    from harness import ToolOutput, ToolSpec
    reg.register(ToolSpec(name="ext_demo", brief="演示扩展工具", core=False,
                          parameters={"type": "object", "properties": {}, "required": []},
                          handler=lambda: ToolOutput(content="ext ok")))
    names = [s["function"]["name"] for s in reg.schemas()]
    check("扩展工具默认不在 schema 中", "ext_demo" not in names)
    out = reg.execute("ext_demo", {})
    check("未加载扩展工具被拒并给提示", not out.ok and "load_tools" in out.hint)
    reg.execute("load_tools", {"names": ["ext_demo"]})
    out = reg.execute("ext_demo", {})
    check("加载后扩展工具可调用", out.ok and out.content == "ext ok")

    # 10. 超长结果截断并外置
    reg.register(ToolSpec(name="long_result", brief="返回超长结果", core=True,
                          parameters={"type": "object", "properties": {}, "required": []},
                          handler=lambda: ToolOutput(content="x" * 20000)))
    out = reg.execute("long_result", {})
    check("超长结果标注未完整并外置", "[结果未完整]" in out.content and "read_file" in out.content)

    # 11. 交接文件写读闭环
    handoff.write(done="完成 A", next_steps="继续 B", open_questions="缺少 pytest")
    check("交接可写可读", "完成 A" in handoff.read() and "缺少 pytest" in handoff.read())

    print()
    if failures:
        print(f"共 {len(failures)} 项失败: {failures}")
        sys.exit(1)
    print("全部通过。")


if __name__ == "__main__":
    main()
