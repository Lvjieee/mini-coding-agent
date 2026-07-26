"""Tool call offloading (harness 组件)。

工具输出超过阈值时，不再粗暴截断丢失信息，而是保留头尾内联 +
完整内容卸载到文件系统 + 返回一个 read_file 可取回的路径引用。
"""
from __future__ import annotations
import time
import uuid
from pathlib import Path

OFFLOAD_THRESHOLD_CHARS = 2000   # 超过这个字符数就卸载
HEAD_CHARS = 800                  # 保留头部多少字符
TAIL_CHARS = 600                  # 保留尾部多少字符


def _offload_dir(root_dir: Path) -> Path:
    d = root_dir / "workspace" / "tool_outputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def maybe_offload(output: str, *, root_dir: Path, tool_name: str = "tool") -> str:
    """小输出原样返回；大输出卸载到文件，只留头尾 + read_file 提示。"""
    if output is None:
        return "[no output]"
    if len(output) <= OFFLOAD_THRESHOLD_CHARS:
        return output if output.strip() else "[no output]"

    file_id = f"{tool_name}_{int(time.time())}_{uuid.uuid4().hex[:6]}.txt"
    out_path = _offload_dir(root_dir) / file_id
    try:
        out_path.write_text(output, encoding="utf-8")
        rel_path = out_path.relative_to(root_dir).as_posix()
    except Exception as exc:
        return output[:OFFLOAD_THRESHOLD_CHARS] + f"\n...[截断；卸载失败: {exc}]"

    head = output[:HEAD_CHARS]
    tail = output[-TAIL_CHARS:]
    omitted = len(output) - HEAD_CHARS - TAIL_CHARS
    return (
        f"{head}\n"
        f"\n...[省略 {omitted} 字符 — 完整输出已卸载]...\n\n"
        f"{tail}\n"
        f"\n[完整输出保存在: {rel_path} — 需要中间部分请用 read_file 读取。]"
    )