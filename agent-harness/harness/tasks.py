"""任务状态：行为级验收清单 + 跨会话交接。

借鉴 Anthropic 长任务 harness 的两条经验：
- 任务开始就拆成「具体行为描述」的验收清单（JSON），每条 pass/fail；
  禁止删除条目、禁止修改行为描述（防止接近上限时降低完成标准）；
  标记 pass 必须附可核查的证据（对抗模型自我评估偏乐观）。
- 进度/交接文件 + 版本历史，让下一个会话（或下一个 Agent）能恢复现场，
  避免留下"缺少说明的半成品"。
"""
from __future__ import annotations

import json
import os
import time


class Checklist:
    """验收清单。只提供 add / mark 两个修改入口：条目不可删除，行为描述不可修改。"""

    def __init__(self, path: str):
        self.path = path
        self.items: list[dict] = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.items = json.load(f)

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=1)

    def add(self, behavior: str) -> str:
        item_id = f"F{len(self.items) + 1:03d}"
        self.items.append({
            "id": item_id,
            "behavior": behavior.strip(),
            "status": "pending",  # pending / pass / fail
            "evidence": "",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._save()
        return item_id

    def bulk_add(self, behaviors: list[str]) -> list[str]:
        return [self.add(b) for b in behaviors if b.strip()]

    def mark(self, item_id: str, status: str, evidence: str = "") -> str:
        if status not in ("pass", "fail", "pending"):
            return "status 必须是 pass / fail / pending。"
        item = next((it for it in self.items if it["id"] == item_id), None)
        if item is None:
            return f"条目 {item_id} 不存在。用 checklist_view 查看当前清单。"
        if status == "pass" and len(evidence.strip()) < 10:
            return ("拒绝：标记 pass 必须附可核查的证据"
                    "（运行了什么命令、输出是什么、结果文件在哪里）。")
        item["status"] = status
        item["evidence"] = evidence.strip()
        self._save()
        return f"{item_id} → {status}"

    def unresolved(self) -> list[dict]:
        return [it for it in self.items if it["status"] != "pass"]

    def render(self) -> str:
        if not self.items:
            return "(清单为空)"
        symbol = {"pass": "[x]", "fail": "[!]", "pending": "[ ]"}
        lines = []
        for it in self.items:
            line = f"{symbol[it['status']]} {it['id']} {it['behavior']}"
            if it["evidence"]:
                line += f"\n      证据: {it['evidence'][:200]}"
            lines.append(line)
        lines.append("规则：条目不可删除、行为描述不可修改；标记 pass 必须附证据。")
        return "\n".join(lines)


class Handoff:
    """跨会话交接文件：已完成什么、下一步做什么、还有什么没解决。"""

    def __init__(self, path: str):
        self.path = path

    def write(self, done: str, next_steps: str, open_questions: str = "", notes: str = "") -> str:
        content = (
            f"# 任务交接（{time.strftime('%Y-%m-%d %H:%M:%S')}）\n\n"
            f"## 已完成\n{done.strip() or '（无）'}\n\n"
            f"## 下一步\n{next_steps.strip() or '（无）'}\n\n"
            f"## 未解决问题 / 环境缺失\n{open_questions.strip() or '（无）'}\n"
        )
        if notes.strip():
            content += f"\n## 备注\n{notes.strip()}\n"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(content)
        return self.path

    def read(self) -> str:
        if not os.path.exists(self.path):
            return ""
        with open(self.path, encoding="utf-8") as f:
            return f.read().strip()
