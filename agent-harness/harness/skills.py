"""Skill：一类任务的做法（流程、约束、脚本、验收标准）。

与 Memory 的分工：经过验证的工作方法放这里（可版本化、可评审、可回滚），
用户事实和历史状态放 Memory。

渐进式加载：上下文里默认只有名称 + 简介；模型确认适用后再 load 完整 SKILL.md。
"""
from __future__ import annotations

import os


class SkillLibrary:
    def __init__(self, root: str):
        self.root = root
        self._index: dict[str, dict] = {}
        self.refresh()

    def refresh(self):
        self._index.clear()
        if not os.path.isdir(self.root):
            return
        for name in sorted(os.listdir(self.root)):
            path = os.path.join(self.root, name, "SKILL.md")
            if not os.path.isfile(path):
                continue
            self._index[name] = {
                "path": path,
                "description": self._parse_description(path),
            }

    @staticmethod
    def _parse_description(path: str) -> str:
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.lower().startswith("description:"):
                    return stripped.split(":", 1)[1].strip()
                if stripped and not stripped.startswith("#") and ":" not in stripped[:20]:
                    return stripped[:120]
        return "(无简介)"

    def index_text(self) -> str:
        if not self._index:
            return "(暂无已安装的 Skill)"
        return "\n".join(f"- {name}: {meta['description']}" for name, meta in self._index.items())

    def load(self, name: str) -> str | None:
        meta = self._index.get(name)
        if meta is None:
            return None
        with open(meta["path"], encoding="utf-8") as f:
            return f.read()

    @property
    def names(self) -> list[str]:
        return list(self._index)
