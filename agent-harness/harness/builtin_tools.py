"""内置工具：文件系统、命令执行、清单、交接、记忆、Skill。

设计要点：
- 按用户意图组织工具，不照搬底层 API；
- 每个工具说明何时调用、参数怎么填、结果怎么继续处理；
- 错误返回可纠正信息（文件未找到给同目录候选、编辑失败提示重读、命令报错回传完整 stderr）。
"""
from __future__ import annotations

import difflib
import os
import re
import subprocess

from .guards import StaleFileError, UnreadFileError
from .policy import Risk
from .tools import ToolOutput, ToolRegistry, ToolSpec

_SKIP_DIRS = {".git", ".harness", "node_modules", "__pycache__", ".venv", "venv"}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


def register_builtin(
    reg: ToolRegistry,
    checklist=None,
    handoff=None,
    memory=None,
    skills=None,
    readonly: bool = False,
):
    """注册内置工具。readonly=True 时只注册只读工具（供独立验收 Agent 使用）。"""
    ctx = reg.ctx

    def _abs(path: str) -> str:
        return path if os.path.isabs(path) else os.path.join(ctx.workspace, path)

    # ---------- 文件系统 ----------

    def read_file(path: str, offset: int = 1, limit: int = 400) -> ToolOutput:
        p = _abs(path)
        decision = ctx.policy.check_path(p, Risk.READ)
        if not decision.allowed:
            return ToolOutput(ok=False, content=decision.reason, hint="改用工作区内路径。")
        if not os.path.isfile(p):
            parent = os.path.dirname(p) or ctx.workspace
            candidates = []
            if os.path.isdir(parent):
                candidates = difflib.get_close_matches(
                    os.path.basename(p), os.listdir(parent), n=5, cutoff=0.4)
            hint = "用 list_dir 或 search_text 确认路径。"
            if candidates:
                hint += f" 同目录相近文件: {', '.join(candidates)}"
            return ToolOutput(ok=False, content=f"文件不存在: {path}", hint=hint)
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        ctx.guard.record_read(p)
        offset = max(1, offset)
        seg = lines[offset - 1: offset - 1 + limit]
        body = "\n".join(f"{i + offset}|{line}" for i, line in enumerate(seg))
        note = f"\n[共 {len(lines)} 行，当前显示第 {offset}-{offset + len(seg) - 1} 行]"
        if offset - 1 + limit < len(lines):
            note += f"（未完整，可用 offset={offset + limit} 继续读取）"
        return ToolOutput(content=body + note)

    def list_dir(path: str = ".") -> ToolOutput:
        p = _abs(path)
        decision = ctx.policy.check_path(p, Risk.READ)
        if not decision.allowed:
            return ToolOutput(ok=False, content=decision.reason)
        if not os.path.isdir(p):
            return ToolOutput(ok=False, content=f"目录不存在: {path}", hint="用 list_dir(\".\") 从工作区根开始。")
        entries = sorted(os.listdir(p))
        lines = [e + "/" if os.path.isdir(os.path.join(p, e)) else e for e in entries]
        return ToolOutput(content="\n".join(lines) or "(空目录)")

    def search_text(pattern: str, path: str = ".", max_results: int = 100) -> ToolOutput:
        p = _abs(path)
        decision = ctx.policy.check_path(p, Risk.READ)
        if not decision.allowed:
            return ToolOutput(ok=False, content=decision.reason)
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return ToolOutput(ok=False, content=f"正则无效: {e}", hint="修正正则表达式后重试。")
        matches: list[str] = []
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, encoding="utf-8", errors="strict") as f:
                        for lineno, line in enumerate(f, 1):
                            if regex.search(line):
                                rel = os.path.relpath(fpath, ctx.workspace)
                                matches.append(f"{rel}:{lineno}: {line.rstrip()[:200]}")
                                if len(matches) >= max_results:
                                    break
                except (UnicodeDecodeError, OSError):
                    continue
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break
        if not matches:
            return ToolOutput(content="无匹配。可放宽 pattern 或换目录再试。")
        note = f"\n[命中 {len(matches)} 条{'，已达上限，结果可能不完整' if len(matches) >= max_results else ''}]"
        return ToolOutput(content="\n".join(matches) + note)

    def write_file(path: str, content: str) -> ToolOutput:
        p = _abs(path)
        decision = ctx.policy.check_path(p, Risk.WRITE)
        if not decision.allowed:
            return ToolOutput(ok=False, content=decision.reason, retryable=False)
        try:
            ctx.guard.check_write(p)
        except (UnreadFileError, StaleFileError) as e:
            return ToolOutput(ok=False, content=str(e), hint="先 read_file 读取现状再写入。")
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        ctx.guard.record_read(p)
        return ToolOutput(content=f"已写入 {path}（{len(content)} 字符）。", files_changed=[p])

    def edit_file(path: str, old_string: str, new_string: str) -> ToolOutput:
        p = _abs(path)
        decision = ctx.policy.check_path(p, Risk.WRITE)
        if not decision.allowed:
            return ToolOutput(ok=False, content=decision.reason, retryable=False)
        if not os.path.isfile(p):
            return ToolOutput(ok=False, content=f"文件不存在: {path}", hint="先确认路径；新文件用 write_file 创建。")
        try:
            ctx.guard.check_write(p)
        except (UnreadFileError, StaleFileError) as e:
            return ToolOutput(ok=False, content=str(e), hint="先 read_file 读取最新内容，再基于最新内容编辑。")
        with open(p, encoding="utf-8") as f:
            text = f.read()
        count = text.count(old_string)
        if count == 0:
            return ToolOutput(
                ok=False, content="old_string 未在文件中找到。",
                hint="文件内容可能与你的记忆不一致：重新 read_file，用文件中的实际文本作为 old_string。")
        if count > 1:
            return ToolOutput(
                ok=False, content=f"old_string 在文件中出现 {count} 次，无法确定编辑位置。",
                hint="在 old_string 中包含更多上下文行，使其唯一。")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text.replace(old_string, new_string, 1))
        ctx.guard.record_read(p)
        return ToolOutput(content=f"已编辑 {path}。", files_changed=[p])

    def run_command(command: str, timeout: int = 120) -> ToolOutput:
        decision = ctx.policy.check_command(command)
        if not decision.allowed:
            return ToolOutput(ok=False, content=decision.reason, retryable=False,
                              hint="改用被允许的命令，或请用户手工执行并回传结果。")
        if decision.needs_approval and not ctx.approval.request(f"执行命令: {command}"):
            ctx.audit.log("command_denied", command=command)
            return ToolOutput(ok=False, content="该命令有破坏性，需要人工审批且未获批准。",
                              retryable=False, hint="向用户说明意图并请求授权。")
        try:
            proc = subprocess.run(command, shell=True, cwd=ctx.workspace,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ToolOutput(ok=False, content=f"命令超时（>{timeout}s）: {command}",
                              hint="拆成更小的步骤，或提高 timeout 参数。")
        output = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode != 0:
            return ToolOutput(
                ok=False, content=f"退出码 {proc.returncode}\n{output.strip() or '(无输出)'}",
                hint=("根据 stderr 修正后重试。若因缺少依赖/命令/权限而失败，"
                      "这是环境缺失的信号：记录到交接说明，不要凭空绕过。"))
        return ToolOutput(content=output.strip() or "(命令成功，无输出)")

    reg.register(ToolSpec(
        name="read_file", brief="读取文件内容（带行号，支持分页）",
        description="读取工作区内文件。返回带行号的内容；文件较长时用 offset/limit 分页继续读取。修改任何文件前必须先读取。",
        parameters=_obj({
            "path": {"type": "string", "description": "相对工作区的路径"},
            "offset": {"type": "integer", "description": "起始行，默认 1"},
            "limit": {"type": "integer", "description": "读取行数，默认 400"},
        }, ["path"]),
        handler=read_file, risk=Risk.READ))
    reg.register(ToolSpec(
        name="list_dir", brief="列出目录内容",
        parameters=_obj({"path": {"type": "string", "description": "默认工作区根目录"}}, []),
        handler=list_dir, risk=Risk.READ))
    reg.register(ToolSpec(
        name="search_text", brief="在工作区内用正则搜索文本",
        description="路径不明或需要定位代码时先搜索。返回 文件:行号: 内容。命中达到上限时结果不完整，应缩小范围再搜。",
        parameters=_obj({
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索目录，默认工作区根"},
            "max_results": {"type": "integer", "description": "默认 100"},
        }, ["pattern"]),
        handler=search_text, risk=Risk.READ))
    reg.register(ToolSpec(
        name="run_command", brief="在工作区执行 shell 命令",
        description="用于运行测试、构建、脚本等。命令受 allowlist/denylist 约束；破坏性命令需人工审批。失败时会返回完整 stderr，据此修正。",
        parameters=_obj({
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "秒，默认 120"},
        }, ["command"]),
        handler=run_command, risk=Risk.WRITE))

    if not readonly:
        reg.register(ToolSpec(
            name="write_file", brief="创建或整体覆盖一个文件",
            description="写入前必须先 read_file 读取现状（新文件除外）；若文件在读取后被修改过会拒绝写入并要求重读。",
            parameters=_obj({
                "path": {"type": "string"},
                "content": {"type": "string"},
            }, ["path", "content"]),
            handler=write_file, risk=Risk.WRITE))
        reg.register(ToolSpec(
            name="edit_file", brief="对文件做精确字符串替换",
            description="old_string 必须在文件中唯一出现；不唯一时包含更多上下文。编辑前必须先 read_file。",
            parameters=_obj({
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            }, ["path", "old_string", "new_string"]),
            handler=edit_file, risk=Risk.WRITE))

    # ---------- 验收清单 ----------

    if checklist is not None:
        reg.register(ToolSpec(
            name="checklist_view", brief="查看验收清单当前状态",
            parameters=_obj({}, []),
            handler=lambda: ToolOutput(content=checklist.render()), risk=Risk.READ))
        if not readonly:
            reg.register(ToolSpec(
                name="checklist_add", brief="向验收清单追加一条可独立验证的行为描述",
                description="发现遗漏的验收项时追加。注意：清单条目只能增加和标记，不能删除或改写（不得降低标准）。",
                parameters=_obj({"behavior": {"type": "string"}}, ["behavior"]),
                handler=lambda behavior: ToolOutput(content=f"已添加 {checklist.add(behavior)}"),
                risk=Risk.WRITE))
            reg.register(ToolSpec(
                name="checklist_mark", brief="标记清单条目为 pass/fail（pass 必须附证据）",
                description="每完成并验证一项立即标记。evidence 写清运行了什么命令、输出是什么、结果文件在哪里；证据不足会被拒绝。",
                parameters=_obj({
                    "item_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["pass", "fail", "pending"]},
                    "evidence": {"type": "string"},
                }, ["item_id", "status"]),
                handler=lambda item_id, status, evidence="": ToolOutput(
                    content=checklist.mark(item_id, status, evidence)),
                risk=Risk.WRITE))

    # ---------- 交接 / 记忆 / Skill ----------

    if handoff is not None and not readonly:
        reg.register(ToolSpec(
            name="write_handoff", brief="写任务交接记录（已完成/下一步/未解决问题）",
            description="长任务推进中或收尾时更新。下一个会话依靠它恢复现场，请写清已完成什么、下一步做什么、缺什么环境。",
            parameters=_obj({
                "done": {"type": "string"},
                "next_steps": {"type": "string"},
                "open_questions": {"type": "string"},
                "notes": {"type": "string"},
            }, ["done", "next_steps"]),
            handler=lambda done, next_steps, open_questions="", notes="": ToolOutput(
                content=f"交接已写入 {handoff.write(done, next_steps, open_questions, notes)}"),
            risk=Risk.WRITE))

    if memory is not None and not readonly:
        reg.register(ToolSpec(
            name="remember", brief="写入一条陈述性记忆（经过准入判断）",
            description=("只存陈述性信息：fact(稳定事实)/background(知识背景)/behavior(行为信号)/"
                         "style(表达偏好)/session(会话延续)。做事方法不要存记忆，应沉淀为 Skill。"
                         "scope: session/workspace/user。"),
            parameters=_obj({
                "kind": {"type": "string", "enum": list("fact background behavior style session".split())},
                "scope": {"type": "string", "enum": ["session", "workspace", "user"]},
                "content": {"type": "string"},
            }, ["kind", "scope", "content"]),
            handler=lambda kind, scope, content: ToolOutput(content=memory.admit(kind, scope, content)),
            risk=Risk.WRITE))

    if skills is not None:
        reg.register(ToolSpec(
            name="load_skill", brief="加载一个 Skill 的完整流程说明",
            description="任务与某个 Skill 简介匹配时加载完整 SKILL.md，并按其流程与完成标准执行。",
            parameters=_obj({"name": {"type": "string", "description": "Skill 名称"}}, ["name"]),
            handler=lambda name: (
                ToolOutput(content=skills.load(name))
                if skills.load(name) is not None
                else ToolOutput(ok=False, content=f"Skill {name} 不存在。",
                                hint="可用 Skill：\n" + skills.index_text())
            ),
            risk=Risk.READ))
