"""完成防线：对抗 Agent「过早宣布完成」这一核心失效模式。

从 `AgentLoop` 里抽出来独立成类，原因是要让**两种 runtime 共用同一套验收逻辑**：
自带循环的 `AgentLoop` 和外包给 Claude Code 的 `ClaudeCodeRuntime` 都调用它。
只有验收逻辑完全相同，A/B 对比"换 runtime 后防线收益是否成立"才是公平的。

三道关卡按"先便宜后昂贵"排序：
1. pre_done 传感器 —— 计算型（跑命令/脚本），确定性强、成本近乎为零；
2. 验收清单 —— 检查是否还有未标记 pass 的行为级条目；
3. 独立 Evaluator —— 推断型，全新上下文的只读子代理，最贵，只在前两关都过了才动用。
"""
from __future__ import annotations

from .builtin_tools import register_builtin
from .sensors import SensorBank
from .subagent import parse_evaluator_output, run_subagent
from .tools import ToolRegistry


class CompletionDefense:
    def __init__(
        self,
        sensors: SensorBank,
        checklist,
        tool_ctx,
        client=None,
        evaluator_client=None,
        audit=None,
        use_evaluator: bool = True,
    ):
        self.sensors = sensors
        self.checklist = checklist
        self.tool_ctx = tool_ctx
        self.client = client
        self.evaluator_client = evaluator_client
        self.audit = audit if audit is not None else getattr(tool_ctx, "audit", None)
        # 关掉后只保留计算型信号——用于消融实验，隔离出「传感器」这一层的独立贡献
        self.use_evaluator = use_evaluator

    # ---------- 对外入口 ----------

    def check(self, goal: str) -> list[str]:
        """返回未达标的问题列表；空列表表示可以交付。"""
        problems: list[str] = []

        feedback = SensorBank.failures_to_feedback(self.sensors.run("pre_done"))
        if feedback:
            problems.append(feedback)

        unresolved = self.checklist.unresolved() if self.checklist else []
        if unresolved:
            lines = "\n".join(
                f"- [{it['status']}] {it['id']} {it['behavior']}" for it in unresolved)
            problems.append("验收清单尚有未通过条目（逐项完成并附证据标记 pass）：\n" + lines)

        # 计算型信号全部通过后，才动用更贵的推断型验收
        if not problems and self.use_evaluator and (self.evaluator_client or self.client):
            accepted, issues = self.evaluate(goal)
            if not accepted:
                problems.append("独立验收未通过：\n" + "\n".join(f"- {i}" for i in issues))

        return problems

    def evaluate(self, goal: str) -> tuple[bool, list[str]]:
        """独立验收 Agent：全新上下文、只读工具、可用不同模型。

        关键设计：不把实现者的结论当输入前提。验收者拿到目标和清单后，
        必须自己用只读工具去核查，解析失败按"未通过"处理（fail-closed）。
        """
        eval_registry = ToolRegistry(self.tool_ctx)
        register_builtin(eval_registry, checklist=self.checklist, readonly=True)
        task = (
            f"任务目标：\n{goal}\n\n"
            f"验收清单（实现者标记的状态与证据仅供参考，须实际核查）：\n"
            f"{self.checklist.render() if self.checklist else '（无清单）'}\n\n"
            f"工作区：{self.tool_ctx.workspace}"
        )
        output = run_subagent(
            self.evaluator_client or self.client, "evaluator", task, eval_registry)
        accepted, issues = parse_evaluator_output(output)
        if self.audit is not None:
            self.audit.log("evaluation", accepted=accepted,
                           issues=[i[:300] for i in issues])
        return accepted, issues

    # ---------- 打回时给模型的修正信息 ----------

    @staticmethod
    def rejection_message(problems: list[str]) -> str:
        """把问题列表组装成重新注入目标的文本（Ralph-Loop 的"回传修正"环节）。

        明确禁止降低标准和删条目——否则模型会倾向于改清单而不是改代码。
        """
        return (
            "任务尚未达到完成标准，请继续执行（不得降低标准、不得删除清单条目）：\n\n"
            + "\n\n".join(problems)
        )
