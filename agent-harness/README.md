<div align="center">

# Agent Harness

**可审计的 Agent 执行控制系统 — 让不可靠的模型产出可靠的交付**

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/依赖-标准库-success)
![Model](https://img.shields.io/badge/模型-OpenAI兼容-412991?logo=openai&logoColor=white)
![Eval](https://img.shields.io/badge/烂尾交付率-78%25→0%25-3fb950)

</div>

思路仿照三家实践：腾讯 WorkBuddy（五层 Harness 控制系统）、Anthropic（长任务 harness 与
对抗式验收）、OpenAI Codex（规则文件 + 确定性检查 + 环境缺失信号）。
纯 Python 标准库实现，无框架依赖；模型接入任何 OpenAI 兼容接口。

> **核心立场**：模型是无状态函数，可靠性来自包在模型外面的控制系统。
> 行动前用前馈（Feedforward）给足目标、规则、环境和能力；
> 行动后用反馈传感器（Feedback sensors）观察结果，把错误和修正信息回传给模型。

## ✨ 亮点

- **完成防线**：pre_done 传感器 + 行为级验收清单 + 独立 Evaluator + 打回重注入，
  对抗「过早宣布完成」这一核心失效模式。
- **可量化收益**：弱模型 × 9 任务 × 每臂 27 次运行，**烂尾交付率 78% → 0%**，真完成 5 → 9，
  代价约 3.3× 轮次 → 见 [`eval/`](eval/README.md)。
- **纯前馈/反馈解耦**：上下文只追加（Prompt Cache 友好）、传感器计算型优先、验收器 fail-closed。
- **零依赖**：仅 Python 标准库，`urllib` 直连模型网关，易审计、易嵌入。
- **可插拔执行层**：同一套完成判定可驱动自带 `AgentLoop`，也可套在 Claude Code CLI 外面
  （`claude_runtime.py`）；装配示例见 [`examples/ci_gate.py`](examples/ci_gate.py)。

## 架构

```
                    ┌─────────────────────────────────────────┐
                    │            AgentLoop (loop.py)          │
                    │  ReAct 循环 · 完成防线 · 预算 · 压缩 · 交接 │
                    └───────┬─────────────────────┬───────────┘
        前馈 Feedforward     │                     │     反馈 Feedback
  ┌─────────────────────────┴──┐               ┌──┴──────────────────────────┐
  │ ContextBuilder (context.py)│               │ SensorBank (sensors.py)     │
  │  稳定前缀: 系统提示+规则文件  │               │  post_edit / pre_done /     │
  │  动态追加: 环境+记忆+清单     │               │  periodic 计算型检查         │
  │ SkillLibrary (skills.py)   │               │ FileGuard (guards.py)       │
  │ MemoryStore (memory.py)    │               │  时间戳写入保护               │
  │  五类陈述性记忆+准入判断      │               │ Evaluator (subagent.py)     │
  └────────────────────────────┘               │  独立验收(推断型,最后动用)     │
  ┌────────────────────────────────────────────┴─────────────────────────────┐
  │ 执行与约束: ToolRegistry (tools.py) + builtin_tools.py                    │
  │  渐进式加载 · 结果截断外置 · 可纠正错误 · Policy/ApprovalGate/AuditLog      │
  └──────────────────────────────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ 任务状态: Checklist + Handoff (tasks.py) — 行为级验收清单 · 跨会话交接      │
  └──────────────────────────────────────────────────────────────────────────┘
```

## 与三家实践的对应

**WorkBuddy 五层 Harness**

- 运行环境层 → `policy.py`（工作区边界、命令 allowlist/denylist、审批门）、`audit.py`（JSONL 审计）
- 引导层（前馈）→ `context.py`（稳定前缀 + 动态追加，Prompt Cache 友好）、`skills.py`、`memory.py`
- 反馈层 → `sensors.py`（计算型优先、按时机分层）、`guards.py`（编辑前时间戳校验）、
  工具错误带失败原因/是否可重试/建议下一步（`tools.py`）
- 编排层 → 工具与 Skill 渐进式加载（`search_tools`/`load_tools`/`load_skill`）、子代理隔离（`subagent.py`）
- 迭代层 → 传感器、规则文件、Skill 都是数据和配置，可随模型与失败模式增删

**Anthropic 长任务 harness**

- 行为级验收清单（JSON、pass/fail、禁止删条目和降标准、pass 必须附证据）→ `tasks.py Checklist`
- 进度文件交接、恢复现场 → `tasks.py Handoff`，开跑时注入"上次交接记录"
- Planner / Generator / Evaluator 分离，验收可用不同模型 → `subagent.py` + `loop.py _evaluate`
- 对抗"过早宣布完成"→ 完成防线：pre_done 传感器 + 未清清单 + 独立验收，未达标打回重注入目标

**OpenAI Codex 实践**

- AGENTS.md 作为目录入口、详细知识外置按需查询 → `context.py` 规则文件加载 + `templates/AGENTS.md`
- 确定性检查（lint/结构测试）每次修改后自动运行 → post_edit 传感器
- "Agent 卡住 = 环境缺少工具/规则/文档的信号" → 工具错误提示与交接文件中显式记录环境缺失

**WorkBuddy 记忆设计**

- 五类陈述性记忆（稳定事实/知识背景/行为信号/表达偏好/会话延续），类型与作用域正交
- 准入判断：程序性内容（做事方法）拒绝入库，提示沉淀为 Skill；行为信号置信封顶、重复观察升权
- 注入为"记忆卡片"，保留来源与置信度，不作为确定前提；支持纠正、删除、时间衰减

## 快速开始

```bash
export OPENAI_BASE_URL=https://your-gateway/v1   # 任何 OpenAI 兼容接口
export OPENAI_API_KEY=sk-...
export HARNESS_MODEL=your-model
export HARNESS_EVAL_MODEL=another-model          # 可选：验收用不同模型

python examples/run_task.py "把 utils/date.py 里的时区 bug 修掉并补测试" /path/to/workspace
```

运行状态保存在工作区 `.harness/` 下：`checklist.json`（验收清单）、`HANDOFF.md`（交接）、
`memory.json`（记忆）、`audit.jsonl`（审计）、`overflow/`（外置的长结果）。
中断后重跑同一目标即可从交接记录恢复现场。

## 关键设计决策

- **完成防线的顺序**：先跑便宜的计算型信号（传感器、清单状态），全部通过才动用昂贵的
  推断型验收（Evaluator 子代理）。验收 Agent 在全新上下文中用只读工具核查，不轻信实现者结论。
- **fail-closed**：非交互环境下高危操作默认拒绝；验收输出解析失败按"未通过"处理。
- **上下文只追加**：历史消息不改写，保证前缀稳定命中缓存；唯一例外是超阈值压缩，
  把最老的长工具结果外置到文件、窗口内留位置信息。
- **Memory 与 Skill 的分工**：事实进记忆、方法进 Skill。Skill 是文件，可版本化、可评审、可回滚。

## 已知边界（与文章一致）

- 业务正确性验证没有银弹：清单 + 独立验收降低了"实现与测试共享同一误解"的概率，但不能消除；
  核心业务逻辑应降低自治度、保留人工审批。
- 传感器依赖工程本身的可检查性（Harnessability）：老系统先补结构、测试和可观测性，再上 Harness。
- Loop Engineering（定时触发、独立 worktree、跨轮运行）不在本仓库内，
  但 `Checklist`/`Handoff`/`Budget`/传感器就是为被外部 Loop 调度而设计的组件。
