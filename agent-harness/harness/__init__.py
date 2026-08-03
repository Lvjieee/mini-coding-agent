"""一个仿照 WorkBuddy / Anthropic / OpenAI Codex 思路的 Agent Harness。

模型是无状态函数；可靠性来自模型外的控制系统：
前馈（上下文/规则/Skill/记忆）+ 约束（权限/审批/审计）+ 反馈（传感器/写入保护）
+ 编排（渐进式加载/子代理）+ 任务状态（验收清单/交接）。
"""
from .audit import AuditLog
from .builtin_tools import register_builtin
from .context import ContextBuilder
from .claude_runtime import ClaudeCodeRuntime
from .defense import CompletionDefense
from .guards import FileGuard
from .loop import AgentLoop, Budget
from .memory import MemoryStore
from .model import Message, ModelClient, OpenAICompatClient, ToolCall
from .policy import ApprovalGate, Policy, Risk
from .runtime import STATUS_DONE, Runtime, is_delivered
from .sensors import CallableSensor, CommandSensor, SensorBank
from .skills import SkillLibrary
from .streaming import StreamAssembler, assemble, iter_sse_payloads
from .subagent import parse_planner_output, run_subagent
from .tasks import Checklist, Handoff
from .tools import ToolContext, ToolOutput, ToolRegistry, ToolSpec

__all__ = [
    "STATUS_DONE", "AgentLoop", "ApprovalGate", "AuditLog", "Budget",
    "CallableSensor", "Checklist", "ClaudeCodeRuntime", "CommandSensor",
    "CompletionDefense", "ContextBuilder", "FileGuard", "Handoff", "MemoryStore", "Message",
    "ModelClient", "OpenAICompatClient", "Policy", "Risk", "Runtime",
    "SensorBank", "SkillLibrary", "ToolCall", "ToolContext", "ToolOutput",
    "ToolRegistry", "ToolSpec", "StreamAssembler", "assemble",
    "is_delivered", "iter_sse_payloads", "parse_planner_output",
    "register_builtin", "run_subagent",
]
