"""Claude Code CLI runtime with an external completion defense.

Claude Code owns the model/tool loop and its own harness. This runtime adds the
scenario-level layer it does not cover: the process boundary, the retry budget,
the completion decision, and the delivery contract.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from .loop import Budget
from .runtime import STATUS_DONE
from .defense import CompletionDefense


class ClaudeCodeRuntime:
    """Run a goal through Claude Code and verify completion outside the CLI."""

    def __init__(
        self,
        workspace: str,
        defense: CompletionDefense,
        budget: Budget | None = None,
        command: str | list[str] = "claude",
        permission_mode: str = "acceptEdits",
        timeout: int = 600,
        env: dict[str, str] | None = None,
        handoff=None,
    ):
        self.workspace = os.path.abspath(workspace)
        self.defense = defense
        self.budget = budget or Budget()
        self.command = [command] if isinstance(command, str) else list(command)
        self.permission_mode = permission_mode
        self.timeout = timeout
        self.env = {**os.environ, **(env or {})}
        self.handoff = handoff
        self.audit = getattr(defense, "audit", None)

    def run(self, goal: str) -> dict:
        """Run until the defense passes, or return a structured stop result."""
        turns = 0
        rejections = 0
        session_id: str | None = None
        last_summary = ""
        last_meta: dict[str, Any] = {}

        for attempt in range(self.budget.completion_retries + 1):
            prompt = goal if session_id is None else CompletionDefense.rejection_message(
                problems)
            try:
                response = self._invoke(prompt, resume_session=session_id)
            except subprocess.TimeoutExpired:
                return self._stop("error", goal, turns, rejections,
                                  "Claude Code 进程超时。")
            except OSError as exc:
                return self._stop("error", goal, turns, rejections,
                                  f"无法启动 Claude Code: {exc}")

            turns += int(response.get("num_turns") or 1)
            last_summary = str(response.get("result") or response.get("error") or "")
            last_meta = response
            session_id = response.get("session_id") or session_id

            if response.get("error"):
                return self._stop("error", goal, turns, rejections,
                                  last_summary, meta=last_meta)

            problems = self.defense.check(goal)
            if not problems:
                self._log("task_done", turns=turns, rejections=rejections)
                return {
                    "status": STATUS_DONE,
                    "turns": turns,
                    "tool_calls": response.get("tool_calls", 0),
                    "rejections": rejections,
                    "summary": last_summary[:2000],
                    "session_id": session_id,
                    "usage": response.get("usage", {}),
                    "model_usage": response.get("modelUsage", {}),
                    "estimated_cost_usd": response.get("total_cost_usd"),
                    "permission_denials": response.get("permission_denials", []),
                }

            if attempt >= self.budget.completion_retries or not session_id:
                return self._stop(
                    "completion_retries_exhausted", goal, turns, rejections,
                    CompletionDefense.rejection_message(problems),
                    problems=problems, meta=last_meta,
                )

            rejections += 1
            self._log("completion_rejected",
                      problems=[problem[:300] for problem in problems])

        return self._stop("completion_retries_exhausted", goal, turns, rejections,
                          last_summary, meta=last_meta)

    def _build_command(self, prompt: str, resume_session: str | None = None) -> list[str]:
        command = [*self.command, "-p", prompt]
        if resume_session:
            command.extend(["--resume", resume_session])
        command.extend([
            "--permission-mode", self.permission_mode,
            "--output-format", "json",
        ])
        return command

    def _invoke(self, prompt: str, resume_session: str | None = None) -> dict:
        """Invoke the CLI and fail closed when its JSON contract is invalid."""
        proc = subprocess.run(
            self._build_command(prompt, resume_session),
            cwd=self.workspace,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "进程无输出").strip()
            return {"error": f"Claude Code 退出码 {proc.returncode}: {detail[:1000]}"}
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            detail = (proc.stdout or proc.stderr or "进程无输出").strip()
            return {"error": f"Claude Code 输出不是有效 JSON: {exc}; {detail[:500]}"}
        if not isinstance(data, dict):
            return {"error": "Claude Code JSON 顶层结果不是对象。"}
        if data.get("is_error"):
            return {**data, "error": data.get("result") or "Claude Code 返回 is_error=true。"}
        return data

    def _stop(
        self,
        reason: str,
        goal: str,
        turns: int,
        rejections: int,
        summary: str,
        problems: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict:
        unresolved = [item["id"] for item in (self.defense.checklist.unresolved()
                                                if self.defense.checklist else [])]
        if self.handoff is not None:
            self.handoff.write(
                done=self.defense.checklist.render() if self.defense.checklist else "（无清单）",
                next_steps="根据错误信息和未通过清单继续执行",
                open_questions="\n\n".join(problems or []) or summary or reason,
                notes=f"目标：{goal[:300]}\n停止原因：{reason}",
            )
        self._log("task_stopped", reason=reason)
        meta = meta or {}
        return {
            "status": reason,
            "turns": turns,
            "tool_calls": meta.get("tool_calls", 0),
            "rejections": rejections,
            "summary": summary[:2000],
            "unresolved": unresolved,
            "handoff": getattr(self.handoff, "path", None),
            "usage": meta.get("usage", {}),
            "model_usage": meta.get("modelUsage", {}),
            "estimated_cost_usd": meta.get("total_cost_usd"),
            "permission_denials": meta.get("permission_denials", []),
        }

    def _log(self, event: str, **fields):
        if self.audit is not None:
            self.audit.log(event, **fields)
