"""引导层（Feedforward）：Agent 开始前掌握什么 + Prompt Cache 友好的上下文组织。

组织规则（对应 WorkBuddy 的上下文原则）：
- 稳定前缀：System Prompt、长期规则（AGENTS.md/WORKBUDDY.md）保持内容与顺序稳定；
- 动态内容（环境、记忆卡片、Skill 索引、验收清单、交接记录）追加在后面；
- 会话历史只追加，不改写已发送的消息；只有压缩时才接受前缀重算。
"""
from __future__ import annotations

import os
import platform
import time


class ContextBuilder:
    def __init__(
        self,
        workspace: str,
        product: str = "Harness Agent",
        rules_files: tuple[str, ...] = ("AGENTS.md", "WORKBUDDY.md"),
    ):
        self.workspace = workspace
        self.product = product
        self.rules = self._load_rules(rules_files)

    def _load_rules(self, names: tuple[str, ...]) -> str:
        chunks = []
        for n in names:
            p = os.path.join(self.workspace, n)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    chunks.append(f"# 工作区规则（{n}）\n" + f.read().strip())
        return "\n\n".join(chunks)

    def system_prompt(self) -> str:
        """稳定前缀：每次运行内容一致，命中 Prompt Cache。"""
        base = f"""你是 {self.product}，一个在受控 harness 中工作的执行 Agent。

# 工作原则
- 先理解目标再执行；较大任务先看验收清单，一次只推进一项，完成一项立即用证据标记一项。
- 修改文件前必须先读取现状；修改后用可观察的方式验证（运行检查、测试、实际执行），不要凭自我评估宣布完成。
- 路径不明先搜索；互相独立的读取和搜索可以在同一轮并行调用。
- 工具结果标注「结果未完整」时，不要把已见部分当作全部，按提示继续读取。
- 工具报错时，按返回的失败原因和建议下一步修正；卡住时把它当作环境缺失（缺工具/规则/文档）的信号，记录到交接说明，不要凭空绕过。
- 不确定时不猜；需要用户决策的事项，停下来说明并询问。

# 安全边界
- 删除、发送、发布、支付等不可轻易撤销的操作必须经过审批门，未获批准不得执行。
- 只在工作区内读写文件；命令受 allowlist/denylist 约束；被拦截时向用户说明，不要尝试绕过。
- 验收清单条目不可删除、行为描述不可修改（不得降低标准）；标记通过必须附可核查的证据。"""
        if self.rules:
            base += "\n\n" + self.rules
        return base

    def environment_block(self) -> str:
        """动态环境信息：模型不感知时间和运行环境，由产品注入。"""
        return (
            "# 当前环境\n"
            f"操作系统: {platform.system()} {platform.release()}\n"
            f"Python: {platform.python_version()}\n"
            f"工作目录: {self.workspace}\n"
            f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S %z')}"
        )

    def kickoff_message(
        self,
        goal: str,
        checklist=None,
        memory=None,
        skills=None,
        handoff: str = "",
    ) -> str:
        """首条用户消息：环境 + 交接 + 记忆卡片 + Skill 索引 + 验收清单 + 目标。

        全部动态内容集中在这里（稳定前缀之后），后续轮次只追加消息。
        """
        blocks = [self.environment_block()]
        if handoff:
            blocks.append("# 上次任务交接记录（先读这里恢复现场）\n" + handoff)
        if memory is not None:
            cards = memory.render_cards(goal)
            if cards:
                blocks.append(cards)
        if skills is not None:
            blocks.append(
                "# 可用 Skill（渐进式加载：先看简介，确认适用再用 load_skill 读完整流程）\n"
                + skills.index_text()
            )
        if checklist is not None and checklist.items:
            blocks.append("# 验收清单\n" + checklist.render())
        blocks.append("# 任务目标\n" + goal.strip())
        return "\n\n".join(blocks)
