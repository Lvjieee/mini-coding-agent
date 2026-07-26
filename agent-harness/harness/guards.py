"""写入保护：编辑前的时间戳校验。

Agent 改文件前比较「上次读取时间」与「文件最后修改时间」；
若读取之后文件被（用户或其他进程）改过，则拒绝写入并要求重读，避免覆盖最新修改。
"""
from __future__ import annotations

import os


class UnreadFileError(Exception):
    pass


class StaleFileError(Exception):
    pass


class FileGuard:
    def __init__(self):
        self._read_mtime: dict[str, float] = {}

    def record_read(self, path: str):
        real = os.path.realpath(path)
        if os.path.exists(real):
            self._read_mtime[real] = os.path.getmtime(real)

    def check_write(self, path: str):
        real = os.path.realpath(path)
        if not os.path.exists(real):
            return  # 新文件，无覆盖风险
        seen = self._read_mtime.get(real)
        if seen is None:
            raise UnreadFileError(f"{path} 尚未读取。修改前先用 read_file 读取现状。")
        if os.path.getmtime(real) > seen + 1e-6:
            raise StaleFileError(
                f"{path} 在你上次读取之后被修改过。请重新 read_file 后再写入，避免覆盖最新内容。"
            )
