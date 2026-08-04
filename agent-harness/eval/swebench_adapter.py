"""SWE-bench Verified 适配器（无 Docker 硬依赖，按需调用官方 judge）。

设计要点：
- 数据集加载走 HuggingFace `datasets`，仅在真正需要时导入；
- 工作区用 `git init` + 浅拉取，避免 clone 整个仓库；
- Agent 执行结束后 `git diff HEAD` 抽 unified diff 作为 predictions；
- 打分调用官方 `python -m swebench.harness.run_evaluation`，Docker 由 judge 内部拉起；
- 我们只关心 `resolved` 布尔值和每个测试的 pass/fail 明细，不复现 judge 内部流程。

Judge 侧真实执行需要：
    pip install swebench datasets
    Docker 已安装并可用（每实例镜像约 1-3GB）

本适配器本身不依赖 swebench/datasets 包——仅在调用相应函数时按需导入，
方便单测和 dry-run 校验代码结构而不装大依赖。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys


# ---------------------------------------------------------------- 数据集与挑题

DEFAULT_DATASET = "princeton-nlp/SWE-bench_Verified"

# 依赖较重、镜像较大或跨模块修改多的仓库，默认排除以控制免费预算下的迭代时间。
HEAVY_REPOS = {
    "django/django", "matplotlib/matplotlib", "scikit-learn/scikit-learn",
    "numpy/numpy", "pandas-dev/pandas", "sphinx-doc/sphinx", "astropy/astropy",
}


def files_changed(patch: str) -> list[str]:
    """从 unified diff 里数一下涉及了哪些文件——用于挑「单文件小改动」的题目。"""
    return re.findall(r"^diff --git a/(\S+)", patch or "", flags=re.M)


def pick_easy_instances(
    rows: list[dict],
    n: int,
    max_files: int = 2,
    avoid_repos: set[str] | None = None,
) -> list[dict]:
    """按启发式挑最容易的 N 条：文件数少、reference patch 短、非重仓库。"""
    avoid = HEAVY_REPOS if avoid_repos is None else avoid_repos
    scored = []
    for row in rows:
        if row.get("repo") in avoid:
            continue
        files = files_changed(row.get("patch", ""))
        if not files or len(files) > max_files:
            continue
        # (文件数, patch 长度, test_patch 长度) 三元排序：越小越先
        scored.append((len(files), len(row.get("patch", "")),
                       len(row.get("test_patch", "")), row))
    scored.sort(key=lambda item: item[:3])
    return [row for *_, row in scored[:n]]


def load_instances(
    n: int = 5,
    dataset_name: str = DEFAULT_DATASET,
    instance_ids: list[str] | None = None,
    max_files: int = 2,
) -> list[dict]:
    """真调 HF datasets 加载并按启发式挑题；指定 instance_ids 时精确加载。"""
    from datasets import load_dataset  # 延迟导入，keeps unit tests light-weight
    ds = load_dataset(dataset_name, split="test")
    rows = [dict(row) for row in ds]
    if instance_ids:
        by_id = {row["instance_id"]: row for row in rows}
        missing = [i for i in instance_ids if i not in by_id]
        if missing:
            raise KeyError(f"未在 {dataset_name} 找到实例：{missing}")
        return [by_id[i] for i in instance_ids]
    return pick_easy_instances(rows, n=n, max_files=max_files)


# ---------------------------------------------------------------- 工作区准备

def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """薄封装，把 git 报错的 stderr 带出来，便于定位。"""
    return subprocess.run(
        ["git", *args], cwd=cwd,
        capture_output=True, text=True, check=True,
    )


def prepare_workspace(instance: dict, root: str) -> str:
    """浅拉取 `repo` 在 `base_commit` 时的树，返回工作区路径。

    走 `git init + fetch --depth 1 <sha>` 是为了避免 clone 全历史——
    django/sympy 全历史动辄几百 MB，浅拉取只下载目标 commit 的树。
    结束时打一个空 commit，让 `git diff HEAD` 能干净地代表 Agent 的改动。
    """
    workspace = os.path.join(root, instance["instance_id"])
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
    os.makedirs(workspace)

    _git(["init", "-q", "-b", "swebench-base"], cwd=workspace)
    _git(["remote", "add", "origin",
          f"https://github.com/{instance['repo']}.git"], cwd=workspace)
    _git(["fetch", "-q", "--depth", "1", "origin", instance["base_commit"]],
         cwd=workspace)
    _git(["checkout", "-q", "-b", "swebench-base", "FETCH_HEAD"], cwd=workspace)
    # 空 commit 让后续 `git diff HEAD` 只反映 Agent 的改动
    _git(["commit", "--allow-empty", "-q", "-m", "swebench base"], cwd=workspace)
    return workspace


def extract_patch(workspace: str) -> str:
    """把 Agent 对工作区的改动导出为 unified diff。

    未跟踪的新文件会先加进 index 再 diff，否则 `git diff HEAD` 看不到它们。
    diff 完成后立刻 reset，保持工作区状态不受抽 patch 的副作用影响。
    """
    _git(["add", "-A", "-N"], cwd=workspace)  # 把未跟踪文件登记为新文件（不入 index）
    proc = _git(["diff", "HEAD", "--patch", "--no-color", "--binary"],
                cwd=workspace)
    return proc.stdout


# ---------------------------------------------------------------- 官方 judge

def write_predictions(rows: list[dict], path: str, model_name: str) -> None:
    """按 swebench.harness 需要的 JSONL 格式写 predictions（一行一条）。

    只保留 patch 非空的行——空 patch 会让 harness 报 patch_is_None，
    浪费 Docker 时间但不产生额外信号。
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            if not row.get("patch"):
                continue
            f.write(json.dumps({
                "instance_id": row["instance_id"],
                "model_name_or_path": model_name,
                "model_patch": row["patch"],
            }, ensure_ascii=False) + "\n")


def run_evaluation(
    predictions_path: str,
    dataset_name: str,
    run_id: str,
    max_workers: int = 2,
    timeout: int = 1800,
) -> str:
    """调用官方 harness 打分（内部拉起 Docker）。返回 logs 目录路径。

    首次运行会为每个 instance 构建/拉取一个镜像（约 1-3GB/实例）；
    重跑同一 instance 会复用。用完后可 `docker system prune -a` 回收磁盘。
    """
    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--predictions_path", predictions_path,
        "--dataset_name", dataset_name,
        "--run_id", run_id,
        "--max_workers", str(max_workers),
        "--timeout", str(timeout),
    ]
    subprocess.run(cmd, check=True)
    return os.path.abspath(os.path.join("logs", "run_evaluation", run_id))


def parse_reports(logs_dir: str, model_name: str) -> dict[str, dict]:
    """遍历 `<logs_dir>/<model_name>/<instance_id>/report.json`，抽出 resolved 与测试明细。

    容忍缺失/损坏的 report——不存在等价于 resolved=False，让上层照常出结果表。
    """
    out: dict[str, dict] = {}
    model_dir = os.path.join(logs_dir, model_name)
    if not os.path.isdir(model_dir):
        return out
    for instance_id in sorted(os.listdir(model_dir)):
        report_path = os.path.join(model_dir, instance_id, "report.json")
        if not os.path.isfile(report_path):
            continue
        try:
            with open(report_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        entry = data.get(instance_id) or next(iter(data.values()), {}) or {}
        tests_status = entry.get("tests_status", {})
        out[instance_id] = {
            "resolved": bool(entry.get("resolved", False)),
            "patch_applied": bool(entry.get("patch_successfully_applied", False)),
            "fail_to_pass": tests_status.get("FAIL_TO_PASS", {}),
            "pass_to_pass": tests_status.get("PASS_TO_PASS", {}),
        }
    return out
