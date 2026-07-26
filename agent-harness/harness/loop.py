"""主循环：ReAct + 反馈闭环 + 完成防线 + 预算与交接。

针对文章指出的三类长任务典型失败：
- 一次承担过多：验收清单一次只推进一项；预算耗尽时写交接而不是硬撑；
- 过早宣布完成：完成防线 = pre_done 传感器 + 未清清单 + 独立 Evaluator，
  未达标时重新注入目标与问题清单继续执行（Ralph Loop 式拦截提前结束）；
- 上下文膨胀：超过阈值时把最老的长工具结果外置到文件，窗口内只留位置信息
  （压缩是唯一允许改写历史消息、接受缓存重算的场合）。
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from .context import ContextBuilder
from .model import Message, ModelClient
from .sensors import SensorBank
from .subagent import parse_evaluator_output, run_subagent
from .tools import ToolRegistry, render_result
from .builtin_tools import register_builtin


@dataclass
class Budget:
    max_turns: int = 60
    max_tool_calls: int = 240
    completion_retries: int = 3       # 完成防线打回的次数上限
    max_context_chars: int = 300_000  # 触发压缩的阈值（粗略字符数）


class AgentLoop:
    def __init__(
        self,
        client: ModelClient,
        context: ContextBuilder,
        registry: ToolRegistry,
        sensors: SensorBank,
        checklist,
        handoff,
        memory=None,
        skills=None,
        evaluator_client: ModelClient | None = None,
        budget: Budget | None = None,
        enable_defense: bool = True,  # 关闭 = 无条件接受模型的完成宣称（用于对照实验）
    ):
        self.client = client
        self.context = context
        self.registry = registry
        self.sensors = sensors
        self.checklist = checklist
        self.handoff = handoff
        self.memory = memory
        self.skills = skills
        self.evaluator_client = evaluator_client
        self.budget = budget or Budget()
        self.enable_defense = enable_defense
        self.audit = registry.ctx.audit

    # ---------- 入口 ----------

    def run(self, goal: str) -> dict:
        system = Message("system", self.context.system_prompt())
        history: list[Message] = [Message("user", self.context.kickoff_message(
            goal, checklist=self.checklist, memory=self.memory,
            skills=self.skills, handoff=self.handoff.read(),
        ))]
        self.audit.log("task_start", goal=goal[:500])

        turns = 0
        tool_calls = 0
        retries_left = self.budget.completion_retries

        while turns < self.budget.max_turns:
            turns += 1
            self._compact(history)
            msg = self.client.complete([system] + history, self.registry.schemas())
            history.append(msg)

            if msg.tool_calls:
                changed: list[str] = []
                for tc in msg.tool_calls:
                    tool_calls += 1
                    out = self.registry.execute(tc.name, tc.arguments)
                    changed.extend(out.files_changed)
                    history.append(Message("tool", render_result(out), tool_call_id=tc.id))
                if changed:
                    # 反馈左移：每次修改后立刻跑计算型传感器，把失败信息回传给模型自我纠正
                    feedback = SensorBank.failures_to_feedback(
                        self.sensors.run("post_edit", changed))
                    if feedback:
                        history.append(Message("user", feedback))
                if tool_calls >= self.budget.max_tool_calls:
                    return self._stop("budget_tool_calls", goal, history,
                                      turns=turns, tool_calls=tool_calls,
                                      rejections=self.budget.completion_retries - retries_left)
                continue

            # 模型没有再调用工具 → 视为宣称完成，进入完成防线
            problems = self._completion_defense(goal) if self.enable_defense else []
            if not problems:
                return self._finish(goal, msg.content, turns, tool_calls,
                                    rejections=self.budget.completion_retries - retries_left)
            if retries_left <= 0:
                return self._stop("completion_retries_exhausted", goal, history, problems,
                                  turns=turns, tool_calls=tool_calls,
                                  rejections=self.budget.completion_retries)
            retries_left -= 1
            self.audit.log("completion_rejected", problems=[p[:300] for p in problems])
            history.append(Message("user",
                "任务尚未达到完成标准，请继续执行（不得降低标准、不得删除清单条目）：\n\n"
                + "\n\n".join(problems)))

        return self._stop("budget_turns", goal, history,
                          turns=turns, tool_calls=tool_calls,
                          rejections=self.budget.completion_retries - retries_left)

    # ---------- 完成防线 ----------

    def _completion_defense(self, goal: str) -> list[str]:
        problems: list[str] = []
        feedback = SensorBank.failures_to_feedback(self.sensors.run("pre_done"))
        if feedback:
            problems.append(feedback)
        unresolved = self.checklist.unresolved()
        if unresolved:
            lines = "\n".join(f"- [{it['status']}] {it['id']} {it['behavior']}" for it in unresolved)
            problems.append("验收清单尚有未通过条目（逐项完成并附证据标记 pass）：\n" + lines)
        # 计算型信号全部通过后，才动用更贵的推断型验收
        if not problems and (self.evaluator_client or self.client):
            accepted, issues = self._evaluate(goal)
            if not accepted:
                problems.append("独立验收未通过：\n" + "\n".join(f"- {i}" for i in issues))
        return problems

    def _evaluate(self, goal: str) -> tuple[bool, list[str]]:
        """独立验收 Agent：全新上下文、只读工具、可用不同模型。"""
        eval_registry = ToolRegistry(self.registry.ctx)
        register_builtin(eval_registry, checklist=self.checklist, readonly=True)
        task = (
            f"任务目标：\n{goal}\n\n"
            f"验收清单（实现者标记的状态与证据仅供参考，须实际核查）：\n{self.checklist.render()}\n\n"
            f"工作区：{self.registry.ctx.workspace}"
        )
        output = run_subagent(
            self.evaluator_client or self.client, "evaluator", task, eval_registry)
        accepted, issues = parse_evaluator_output(output)
        self.audit.log("evaluation", accepted=accepted, issues=[i[:300] for i in issues])
        return accepted, issues

    # ---------- 收尾 ----------

    def _finish(self, goal: str, summary: str, turns: int,
                tool_calls: int = 0, rejections: int = 0) -> dict:
        self.handoff.write(
            done=f"任务已完成并通过验收。\n{summary}",
            next_steps="（无）",
            notes=f"目标：{goal[:300]}",
        )
        if self.memory is not None:
            self.memory.admit(
                "session", "workspace",
                f"任务「{goal[:60]}」已完成：{summary[:200]}", source="loop")
        self.audit.log("task_done", turns=turns)
        return {"status": "done", "turns": turns, "tool_calls": tool_calls,
                "rejections": rejections, "summary": summary}

    def _stop(self, reason: str, goal: str, history: list[Message],
              problems: list[str] | None = None,
              turns: int = 0, tool_calls: int = 0, rejections: int = 0) -> dict:
        """预算耗尽 / 打回次数用尽：不硬撑，写清交接后停止，留下可诊断的现场。"""
        unresolved = self.checklist.unresolved()
        next_steps = "\n".join(
            f"- {it['id']} {it['behavior']}" for it in unresolved) or "核对清单后收尾"
        self.handoff.write(
            done=self.checklist.render(),
            next_steps=next_steps,
            open_questions="\n\n".join(problems or []) or f"停止原因：{reason}",
            notes=f"目标：{goal[:300]}\n停止原因：{reason}",
        )
        self.audit.log("task_stopped", reason=reason)
        return {
            "status": reason,
            "turns": turns,
            "tool_calls": tool_calls,
            "rejections": rejections,
            "unresolved": [it["id"] for it in unresolved],
            "handoff": self.handoff.path,
            "summary": (history[-1].content if history else "")[:500],
        }

    # ---------- 压缩 ----------

    def _compact(self, history: list[Message]):
        total = sum(len(m.content) for m in history)
        if total <= self.budget.max_context_chars:
            return
        overflow_dir = self.registry.overflow_dir
        os.makedirs(overflow_dir, exist_ok=True)
        # 从最老的消息开始外置大体积工具结果，直到回到阈值以内；保留最近 10 条不动
        for m in history[:-10]:
            if m.role != "tool" or len(m.content) <= 2000:
                continue
            path = os.path.join(overflow_dir, f"compacted-{uuid.uuid4().hex[:8]}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(m.content)
            rel = os.path.relpath(path, self.registry.ctx.workspace)
            total -= len(m.content)
            m.content = f"[已外置] 原工具结果 {len(m.content)} 字符 → {rel}（需要时用 read_file 查看）"
            if total <= self.budget.max_context_chars:
                break
        self.audit.log("context_compacted", total_chars=total)
