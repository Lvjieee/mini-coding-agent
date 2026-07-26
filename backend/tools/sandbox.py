"""Docker 沙箱执行（harness 安全组件）。

不在主进程执行模型生成的代码，而是每次起一个隔离的 Docker 容器：
- 禁网络（--network none）
- 限内存（--memory）
- 只读根文件系统（--read-only）
- 限 CPU、跑完即焚（--rm）
- 超时杀容器

设计理由：永远不要在主进程执行模型输出。PreToolUse 正则拦截能挡明显危险，
但变量拼接/编码混淆能绕过；容器隔离是兜底——即使代码恶意，也困在容器里。
"""
from __future__ import annotations
import subprocess
import tempfile
import os
from pathlib import Path

SANDBOX_IMAGE = "python:3.11-slim"
SANDBOX_TIMEOUT = 15          # 秒
SANDBOX_MEMORY = "256m"       # 内存上限
SANDBOX_CPUS = "0.5"          # CPU 上限


def run_in_sandbox(code: str) -> str:
    """在隔离 Docker 容器里执行 Python 代码，返回 stdout+stderr。"""
    # 把代码写到临时文件，挂载进容器只读执行
    tmp_dir = tempfile.mkdtemp(prefix="sandbox_")
    code_file = Path(tmp_dir) / "snippet.py"
    code_file.write_text(code, encoding="utf-8")

    cmd = [
        "docker", "run",
        "--rm",                          # 跑完即删容器
        "--network", "none",             # 禁网络
        "--memory", SANDBOX_MEMORY,      # 限内存
        "--cpus", SANDBOX_CPUS,          # 限 CPU
        "--read-only",                   # 根文件系统只读
        "-v", f"{tmp_dir}:/sandbox:ro",  # 代码目录只读挂载
        SANDBOX_IMAGE,
        "python", "/sandbox/snippet.py",
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SANDBOX_TIMEOUT + 5,  # 容器启动有开销，给宽限
        )
        combined = (completed.stdout or "") + (completed.stderr or "")
        return combined.strip() or "[no output]"
    except subprocess.TimeoutExpired:
        return f"Timed out after {SANDBOX_TIMEOUT}s (sandbox killed)."
    except FileNotFoundError:
        return "[sandbox error] Docker not found. Falling back is required."
    finally:
        # 清理临时文件
        try:
            code_file.unlink(missing_ok=True)
            os.rmdir(tmp_dir)
        except Exception:
            pass