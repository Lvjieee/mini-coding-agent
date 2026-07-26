"""审计日志：所有动作留痕、可追溯回放（JSONL，append-only）。"""
from __future__ import annotations

import json
import os
import time


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def log(self, event: str, **fields):
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
