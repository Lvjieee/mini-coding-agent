"""约束层（Constrain）：权限边界、Allowlist/Denylist、Approval Gate。

System Prompt 只能引导，不能强制；真正拦截高危操作的是这里的确定性检查。
持有执行权的是 harness 而不是模型，所以校验必须发生在执行层。
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class Risk(str, Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"    # 有破坏性但通常可恢复（删除工作区文件等）
    IRREVERSIBLE = "irreversible"  # 发送/发布/支付/对外可见，不可轻易撤销


@dataclass
class Decision:
    allowed: bool
    needs_approval: bool = False
    reason: str = ""


DEFAULT_COMMAND_DENY = (
    "rm -rf /", "rm -rf /*", "sudo *", "shutdown*", "reboot*", "mkfs*",
    "chmod -R 777 /*", "curl * | sh*", "curl * | bash*", "wget * | sh*",
    "git push --force*", "git push -f*", "git reset --hard*", "git clean -fd*",
)
DEFAULT_PATH_DENY = (".env", "*.pem", "*.key", "id_rsa*", "*credentials*", "*.p12")


@dataclass
class Policy:
    workspace_root: str
    auto_approve: frozenset = frozenset({Risk.READ, Risk.WRITE})
    command_deny: tuple = DEFAULT_COMMAND_DENY
    command_allow_prefixes: tuple | None = None  # 设定后仅允许这些前缀（denylist 仍生效）
    path_deny: tuple = DEFAULT_PATH_DENY

    def check_path(self, path: str, risk: Risk) -> Decision:
        joined = path if os.path.isabs(path) else os.path.join(self.workspace_root, path)
        real = os.path.realpath(joined)
        root = os.path.realpath(self.workspace_root)
        if not (real == root or real.startswith(root + os.sep)):
            return Decision(False, reason=(
                f"路径越界：{path} 不在工作区 {root} 内。请改用工作区内的相对路径。"
            ))
        base = os.path.basename(real)
        if risk != Risk.READ and any(fnmatch.fnmatch(base, p) for p in self.path_deny):
            return Decision(False, reason=f"{base} 命中敏感文件规则，禁止写入。")
        return Decision(True, needs_approval=risk not in self.auto_approve)

    def check_command(self, command: str) -> Decision:
        cmd = " ".join(command.split())
        for pat in self.command_deny:
            if fnmatch.fnmatch(cmd, pat) or fnmatch.fnmatch(cmd, pat + " *"):
                return Decision(False, reason=(
                    f"命令命中 denylist（{pat}），已拦截。若确有必要，请让用户手工执行。"
                ))
        if self.command_allow_prefixes is not None and not any(
            cmd.startswith(p) for p in self.command_allow_prefixes
        ):
            return Decision(False, reason=(
                "命令不在 allowlist 内。可用前缀: " + ", ".join(self.command_allow_prefixes)
            ))
        destructive_markers = ("rm ", "rmdir ", "git clean", "drop table", "truncate table")
        risk = Risk.DESTRUCTIVE if any(k in cmd.lower() for k in destructive_markers) else Risk.WRITE
        return Decision(True, needs_approval=risk not in self.auto_approve)


class ApprovalGate:
    """危险动作的人工确认。非交互环境下默认拒绝（fail-closed）。"""

    def __init__(self, ask: Callable[[str], bool] | None = None, interactive: bool | None = None):
        self.ask = ask
        if interactive is None:
            try:
                interactive = os.isatty(0)
            except OSError:
                interactive = False
        self.interactive = interactive

    def request(self, description: str) -> bool:
        if self.ask is not None:
            return self.ask(description)
        if not self.interactive:
            return False
        answer = input(f"[审批] 允许执行该操作吗？ {description} [y/N] ").strip().lower()
        return answer in ("y", "yes")
