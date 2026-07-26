"""记忆层：陈述性记忆 + 准入判断 + 作用域分层。

核心是「准入判断」：哪些历史信息有资格继续影响未来的任务。
- 只收五类陈述性记忆（类型回答"存什么"）；
- 作用域回答"在哪里生效"，与类型正交；
- 程序性内容（做事方法）拒绝入库——请沉淀为 Skill（可版本化、可评审、可回滚、按需加载）；
- 行为信号比用户明确表达的偏好更谨慎：置信度封顶，重复观察才升权；
- 注入时以「记忆卡片」形式保留来源与置信度，不当成确定前提。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid

KINDS = {
    "fact": "稳定事实",
    "background": "用户知识背景",
    "behavior": "行为信号",
    "style": "表达偏好",
    "session": "会话延续信息",
}
SCOPES = ("session", "workspace", "user")

_PROCEDURAL_HINTS = ("总是先", "每次都", "以后都", "遇到所有", "必须按以下步骤", "固定流程")


class MemoryStore:
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

    # ---------- 准入 ----------

    def admit(self, kind: str, scope: str, content: str,
              source: str = "agent", confidence: float = 0.6) -> str:
        content = content.strip()
        if kind not in KINDS:
            return f"拒绝写入：kind 必须是 {sorted(KINDS)} 之一（类型回答「存什么」）。"
        if scope not in SCOPES:
            return f"拒绝写入：scope 必须是 {SCOPES} 之一（作用域回答「在哪里生效」）。"
        if not content:
            return "拒绝写入：内容为空。"
        if kind != "session" and any(h in content for h in _PROCEDURAL_HINTS):
            return ("拒绝写入：内容疑似程序性方法（做事步骤）。"
                    "工作方法请沉淀为 Skill（可版本化、可评审、可回滚），Memory 只存陈述性信息。")
        for it in self.items:
            if it["content"] == content and it["scope"] == scope:
                it["confidence"] = round(min(1.0, it["confidence"] + 0.1), 2)  # 重复观察 → 升权
                self._save()
                return f"该记忆已存在（{it['id']}），置信度提升至 {it['confidence']}。"
        if kind == "behavior":
            confidence = min(confidence, 0.4)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        item = {
            "id": uuid.uuid4().hex[:8], "kind": kind, "scope": scope,
            "content": content, "source": source,
            "confidence": round(confidence, 2), "created": now, "last_used": now,
        }
        self.items.append(item)
        self._save()
        return f"已写入记忆 {item['id']}（{KINDS[kind]}·{scope}·置信{item['confidence']}）。"

    # ---------- 检索与注入 ----------

    def retrieve(self, query: str = "", scopes=SCOPES, limit: int = 6) -> list[dict]:
        toks = set(re.findall(r"\w+", query.lower()))

        def score(it: dict) -> float:
            ctoks = set(re.findall(r"\w+", it["content"].lower()))
            overlap = len(toks & ctoks) if toks else 0
            return overlap + it["confidence"]

        ranked = sorted(
            (it for it in self.items if it["scope"] in scopes),
            key=score, reverse=True,
        )[:limit]
        if ranked:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            for it in ranked:
                it["last_used"] = now
            self._save()
        return ranked

    def render_cards(self, query: str = "") -> str:
        items = self.retrieve(query)
        if not items:
            return ""
        lines = [
            f"- [{KINDS[it['kind']]}·{it['scope']}·置信{it['confidence']}·来源:{it['source']}] {it['content']}"
            for it in items
        ]
        return ("# 记忆卡片（仅供参考：保留来源与置信度，不作为确定前提；发现有误请纠正）\n"
                + "\n".join(lines))

    # ---------- 用户纠正与治理 ----------

    def forget(self, item_id: str) -> str:
        before = len(self.items)
        self.items = [it for it in self.items if it["id"] != item_id]
        if len(self.items) == before:
            return f"记忆 {item_id} 不存在。"
        self._save()
        return f"已删除记忆 {item_id}。"

    def correct(self, item_id: str, content: str) -> str:
        for it in self.items:
            if it["id"] == item_id:
                it["content"] = content.strip()
                it["source"] = "user_correction"
                it["confidence"] = 0.9
                self._save()
                return f"已按用户纠正更新记忆 {item_id}。"
        return f"记忆 {item_id} 不存在。"

    def decay(self, days: int = 30, step: float = 0.1):
        """长期未使用的记忆降权，降到 0 以下则删除。"""
        cutoff = time.time() - days * 86400
        kept = []
        for it in self.items:
            last = time.mktime(time.strptime(it["last_used"], "%Y-%m-%d %H:%M:%S"))
            if last < cutoff:
                it["confidence"] = round(it["confidence"] - step, 2)
            if it["confidence"] > 0:
                kept.append(it)
        self.items = kept
        self._save()
