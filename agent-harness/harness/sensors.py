"""反馈层（Feedback）传感器：计算型信号优先，按时机分层。

原则：能用确定性程序解决的问题，优先交给计算型传感器（快、便宜、可重复）；
需要语义判断的问题，交给独立的审查/验收 Agent（见 subagent.py 的 Evaluator）。

分层时机（tier）：
- post_edit: 每次文件修改后立刻运行（lint、类型检查、编译、相关单测）——左移到自我纠正循环里；
- pre_done : 模型宣称完成时运行（构建、全量测试、端到端验证）——完成防线的一部分；
- periodic : 周期性漂移扫描（死代码、依赖风险、文档与代码一致性）——由外部 Loop 调度。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class SensorResult:
    name: str
    ok: bool
    feedback: str = ""


class CommandSensor:
    """运行一条命令，退出码非 0 视为未通过，stderr/stdout 作为修正信息回传模型。"""

    def __init__(
        self,
        name: str,
        command: str,
        tier: str = "post_edit",
        cwd: str = ".",
        timeout: int = 300,
        hint: str = "",
    ):
        assert tier in ("post_edit", "pre_done", "periodic")
        self.name = name
        self.command = command
        self.tier = tier
        self.cwd = cwd
        self.timeout = timeout
        self.hint = hint

    def run(self, changed: list[str] | None = None) -> SensorResult:
        try:
            proc = subprocess.run(
                self.command, shell=True, cwd=self.cwd,
                capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return SensorResult(self.name, False, f"检查命令超时（>{self.timeout}s）: {self.command}")
        if proc.returncode == 0:
            return SensorResult(self.name, True)
        feedback = (proc.stdout[-2000:] + "\n" + proc.stderr[-4000:]).strip()
        if self.hint:
            feedback += f"\n修正提示: {self.hint}"
        return SensorResult(self.name, False, feedback)


class CallableSensor:
    """用 Python 函数实现的传感器：fn(changed) -> (ok, feedback)。"""

    def __init__(self, name: str, fn, tier: str = "post_edit"):
        assert tier in ("post_edit", "pre_done", "periodic")
        self.name = name
        self.fn = fn
        self.tier = tier

    def run(self, changed: list[str] | None = None) -> SensorResult:
        ok, feedback = self.fn(changed or [])
        return SensorResult(self.name, ok, feedback)


class SensorBank:
    def __init__(self):
        self.sensors: list = []

    def add(self, sensor) -> "SensorBank":
        self.sensors.append(sensor)
        return self

    def run(self, tier: str, changed: list[str] | None = None) -> list[SensorResult]:
        return [s.run(changed) for s in self.sensors if s.tier == tier]

    @staticmethod
    def failures_to_feedback(results: list[SensorResult]) -> str | None:
        fails = [r for r in results if not r.ok]
        if not fails:
            return None
        body = "\n\n".join(f"[{r.name}]\n{r.feedback}" for r in fails)
        return "以下自动检查未通过，请先修复再继续（不要降低完成标准）：\n\n" + body
