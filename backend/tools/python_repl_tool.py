from __future__ import annotations
from tools.sandbox import run_in_sandbox

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Type

from tools.hooks import pre_tool_use, post_tool_use, HookBlocked
from tools.tool_offload import maybe_offload   # ✅ 带 tools. 前缀
from langchain_core.callbacks.manager import AsyncCallbackManagerForToolRun, CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class PythonReplInput(BaseModel):
    code: str = Field(..., description="Python code to execute")


class PythonReplTool(BaseTool):
    name: str = "python_repl"
    description: str = "Execute short Python snippets in a subprocess and return stdout/stderr."
    args_schema: Type[BaseModel] = PythonReplInput
    model_config = ConfigDict(arbitrary_types_allowed=True)
    _root_dir: Path = PrivateAttr()

    def __init__(self, root_dir: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._root_dir = root_dir

    def _run(self, code, run_manager=None):
        # === PreToolUse hook：执行前检查 ===
        try:
            pre_tool_use("python_repl", code)
        except HookBlocked as e:
            return str(e)
        # === 在 Docker 沙箱里执行（不在主进程跑模型生成的代码）===
        combined = run_in_sandbox(code)
        # === PostToolUse hook：执行后扫密钥 ===
        combined = post_tool_use("python_repl", combined.strip())
        # === offload ===
        return maybe_offload(combined, root_dir=self._root_dir, tool_name="python_repl")
    async def _arun(
        self,
        code: str,
        run_manager: AsyncCallbackManagerForToolRun | None = None,
    ) -> str:
        return await asyncio.to_thread(self._run, code, None)
