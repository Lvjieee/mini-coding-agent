"""工具层：注册、渐进式加载、结果截断与可纠正错误。

- 渐进式加载：默认只暴露核心工具和工具检索；扩展工具先看名称/简介，
  确认需要再加载完整定义，避免大工具集占满上下文、干扰选择。
- 结果截断：超长结果外置到文件，明确告诉模型「结果未完整」、总量与继续读取方法，
  否则模型会把前一段误当成全部。
- 错误返回：不只给错误码，还给失败原因、是否可重试和建议的下一步。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .audit import AuditLog
from .guards import FileGuard
from .policy import ApprovalGate, Policy, Risk


@dataclass
class ToolContext:
    policy: Policy
    approval: ApprovalGate
    audit: AuditLog
    guard: FileGuard
    workspace: str


@dataclass
class ToolOutput:
    content: str
    ok: bool = True
    hint: str = ""  # 出错时给模型的修正建议
    retryable: bool = True
    files_changed: list[str] = field(default_factory=list)


def render_result(out: ToolOutput) -> str:
    """把工具输出转成回传给模型的文本；失败时附带可纠正信息。"""
    if out.ok:
        return out.content
    parts = [f"[执行失败] {out.content}", f"可重试: {'是' if out.retryable else '否'}"]
    if out.hint:
        parts.append(f"建议下一步: {out.hint}")
    return "\n".join(parts)


@dataclass
class ToolSpec:
    name: str
    brief: str            # 一句话简介，进能力目录
    parameters: dict      # JSON Schema
    handler: Callable[..., ToolOutput]
    description: str = ""  # 完整说明：何时调用、参数怎么填、结果怎么继续处理
    risk: Risk = Risk.READ
    core: bool = True     # core 工具默认在上下文中；非 core 走渐进式发现

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": (self.description or self.brief).strip(),
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    MAX_RESULT_CHARS = 8000

    def __init__(self, ctx: ToolContext, overflow_dir: str = ".harness/overflow"):
        self.ctx = ctx
        self._specs: dict[str, ToolSpec] = {}
        self._loaded: set[str] = set()
        self.overflow_dir = os.path.join(ctx.workspace, overflow_dir)
        self._register_meta()

    def register(self, spec: ToolSpec):
        self._specs[spec.name] = spec

    # ---------- 渐进式加载 ----------

    def catalog(self) -> str:
        lines = [f"- {s.name}: {s.brief}" for s in self._specs.values() if not s.core]
        return "\n".join(lines) or "(无扩展工具)"

    def _register_meta(self):
        self.register(ToolSpec(
            name="search_tools",
            brief="按关键词检索扩展工具目录",
            description=(
                "扩展工具默认不在上下文中。用关键词检索可用工具，返回名称与简介；"
                "确认需要后用 load_tools 加载完整定义再调用。"
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "关键词"}},
                "required": ["query"],
            },
            handler=self._search_tools,
        ))
        self.register(ToolSpec(
            name="load_tools",
            brief="加载扩展工具的完整定义，使其可被调用",
            parameters={
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"}, "description": "工具名列表"}
                },
                "required": ["names"],
            },
            handler=self._load_tools,
        ))

    def _search_tools(self, query: str) -> ToolOutput:
        q = query.lower()
        hits = [
            s for s in self._specs.values()
            if not s.core and (
                q in s.name.lower() or q in s.brief.lower() or q in s.description.lower()
            )
        ]
        if not hits:
            return ToolOutput(content="没有匹配的扩展工具。完整目录：\n" + self.catalog())
        body = "\n".join(f"- {s.name}: {s.brief}" for s in hits)
        return ToolOutput(content=body + "\n用 load_tools 加载后即可调用。")

    def _load_tools(self, names: list[str]) -> ToolOutput:
        loaded, missing = [], []
        for n in names:
            (loaded if n in self._specs else missing).append(n)
        self._loaded.update(loaded)
        msg = f"已加载: {', '.join(loaded) or '无'}"
        if missing:
            msg += f"\n不存在: {', '.join(missing)}。先用 search_tools 确认名称。"
        return ToolOutput(content=msg, ok=not missing)

    def schemas(self) -> list[dict]:
        """当前暴露给模型的工具定义：core + 已按需加载的扩展工具。"""
        return [s.schema() for s in self._specs.values() if s.core or s.name in self._loaded]

    # ---------- 执行：参数校验 → 审批 → 执行 → 审计 → 截断 ----------

    def execute(self, name: str, args: dict) -> ToolOutput:
        spec = self._specs.get(name)
        if spec is None:
            return ToolOutput(
                ok=False, content=f"工具 {name} 不存在。",
                hint="用 search_tools 检索可用工具，用 load_tools 加载。",
            )
        if not spec.core and name not in self._loaded:
            return ToolOutput(
                ok=False, content=f"工具 {name} 尚未加载。",
                hint=f"先调用 load_tools(names=[\"{name}\"]) 加载该工具。",
            )
        missing = [k for k in spec.parameters.get("required", []) if k not in args]
        if missing:
            return ToolOutput(
                ok=False, content=f"缺少必填参数: {', '.join(missing)}",
                hint="补齐参数后重试。",
            )
        if spec.risk not in self.ctx.policy.auto_approve:
            desc = f"{name}({json.dumps(args, ensure_ascii=False)[:200]}) 风险级别={spec.risk.value}"
            if not self.ctx.approval.request(desc):
                self.ctx.audit.log("tool_denied", tool=name, args=args)
                return ToolOutput(
                    ok=False, content="该操作需要人工审批，且未获批准。",
                    hint="向用户说明意图并请求授权，或改用低风险方式完成。",
                    retryable=False,
                )
        try:
            out = spec.handler(**args)
        except TypeError as e:
            out = ToolOutput(ok=False, content=f"参数错误: {e}", hint="检查参数名与类型是否与工具定义一致。")
        except Exception as e:  # 把异常转成可纠正信息回传，而不是让循环中断
            out = ToolOutput(
                ok=False, content=f"{type(e).__name__}: {e}",
                hint="根据错误信息修正后重试；若是环境缺失（缺依赖/命令/权限），记录到交接说明。",
            )
        self.ctx.audit.log("tool_call", tool=name, ok=out.ok,
                           args={k: str(v)[:200] for k, v in args.items()})
        return self._truncate(name, out)

    def _truncate(self, name: str, out: ToolOutput) -> ToolOutput:
        if len(out.content) <= self.MAX_RESULT_CHARS:
            return out
        os.makedirs(self.overflow_dir, exist_ok=True)
        path = os.path.join(self.overflow_dir, f"{name}-{uuid.uuid4().hex[:8]}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(out.content)
        rel = os.path.relpath(path, self.ctx.workspace)
        total = len(out.content)
        out.content = (
            out.content[: self.MAX_RESULT_CHARS]
            + f"\n\n[结果未完整] 共 {total} 字符，仅显示前 {self.MAX_RESULT_CHARS} 字符。"
            + f"完整结果已写入 {rel}，可用 read_file(path=\"{rel}\", offset=N) 继续读取。"
        )
        return out
