"""SWE-bench Verified × A/B 评测 runner。

在 SWE-bench Verified 的一个小子集（默认 5 题）上跑同一 Agent，
只切换「完成防线」开关，观测 baseline/defense 两臂在 `resolved` 与 `false_done` 上的差异。

明确取向：**不冲绝对分数**。免费弱模型（如智谱 glm-4-flash）在 SWE-bench 上
预计 resolved 接近 0；本 runner 的价值是证明 harness 兼容 SWE-bench 判卷协议，
并让 baseline 的假完成流出与 defense 拦截的差距在真实公开 benchmark 上重现。

准备：
    pip install swebench datasets
    export OPENAI_BASE_URL=... OPENAI_API_KEY=... HARNESS_MODEL=glm-4-flash

只准备工作区、抽 patch，不调 Docker judge（用来在没有模型或磁盘不够时先跑通链路）：
    python eval/run_swebench_ab.py --n 3 --skip-judge

真跑完整链路（含 Docker 打分）：
    python eval/run_swebench_ab.py --n 5 --runs 1
    python eval/run_swebench_ab.py --instances "django__django-11039,sympy__sympy-20590"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from harness import (
    AgentLoop, ApprovalGate, AuditLog, Budget, Checklist, CommandSensor,
    ContextBuilder, FileGuard, Handoff, OpenAICompatClient, Policy,
    SensorBank, ToolContext, ToolRegistry, register_builtin,
)

from eval import swebench_adapter as swe


AGENT_MODEL_NAME = "mini-coding-agent"  # 官方 harness 用它作 predictions/logs 子目录名


def build_loop(workspace: str, arm: str, model_client, task: dict):
    """按臂装配 AgentLoop。SWE-bench 场景下：
    - Planner 不用：题目 `problem_statement` 已经是明确的目标
    - Evaluator 子代理不用：Docker 判卷才是最终裁判，评估器再判一次意义不大
    - 防线核心信号是「已有测试仍然全部通过」——即 SWE-bench PASS_TO_PASS 的近似
    """
    state = os.path.join(workspace, ".harness")
    ctx = ToolContext(
        policy=Policy(workspace_root=workspace),
        approval=ApprovalGate(interactive=False),
        audit=AuditLog(os.path.join(state, "audit.jsonl")),
        guard=FileGuard(),
        workspace=workspace,
    )
    defense_on = arm == "defense"
    checklist = Checklist(os.path.join(state, "checklist.json"))
    handoff = Handoff(os.path.join(state, "HANDOFF.md"))
    registry = ToolRegistry(ctx)
    register_builtin(
        registry, checklist=checklist if defense_on else None, handoff=handoff)

    sensors = SensorBank()
    if defense_on:
        # 前置检查：已存在的测试仍然全部通过，作为「不能改坏其它行为」的近似判据
        sensors.add(CommandSensor(
            "existing-tests-still-pass",
            "python -m pytest -x -q --tb=no --no-header --disable-warnings",
            tier="pre_done", cwd=workspace, timeout=600,
            hint=("已有测试出现新的失败——你的修改破坏了其它行为。"
                  "先修好回归项，再宣称完成。"),
        ))
        # 一条通用的行为清单：目标行为存在 + 未破坏其它行为
        checklist.bulk_add([
            f"已根据 issue 定位到根因并对相关文件做出最小必要修改：{task['problem_statement'][:120]}",
            "已在仓库现有测试上跑一遍且全部通过（不引入回归）。",
        ])

    loop = AgentLoop(
        client=model_client,
        context=ContextBuilder(workspace),
        registry=registry,
        sensors=sensors,
        checklist=checklist,
        handoff=handoff,
        evaluator_client=None,
        budget=Budget(max_turns=30, max_tool_calls=120, completion_retries=2),
        enable_defense=defense_on,
    )
    return loop


def run_one(instance: dict, arm: str, run_idx: int, model_client, root: str) -> dict:
    workspace = swe.prepare_workspace(instance, os.path.join(root, arm, str(run_idx)))
    loop = build_loop(workspace, arm, model_client, instance)

    goal = (
        f"仓库：{instance['repo']} @ {instance['base_commit'][:8]}\n"
        f"Issue：{instance['problem_statement']}\n\n"
        "请修改仓库内的源码解决该 issue。"
        "工作区已 checkout 到 base_commit，你的所有改动都会被 `git diff HEAD` 抽为 patch，"
        "交给 SWE-bench 官方 judge 用隐藏测试打分。不要修改测试文件。"
    )
    started = time.time()
    result = loop.run(goal)
    patch = swe.extract_patch(workspace)

    return {
        "instance_id": instance["instance_id"],
        "repo": instance["repo"],
        "arm": arm, "run": run_idx,
        "status": result["status"],
        "claimed_done": result["status"] == "done",
        "rejections": result.get("rejections", 0),
        "turns": result.get("turns", 0),
        "tool_calls": result.get("tool_calls", 0),
        "seconds": round(time.time() - started, 1),
        "patch": patch,
        "workspace": workspace,
    }


def evaluate_arm(rows: list[dict], out_dir: str, arm: str, dataset_name: str) -> dict:
    """把一个臂的 predictions 写文件，调官方 judge 打分，返回 {instance_id: judge}。"""
    predictions_path = os.path.join(out_dir, f"predictions-{arm}.jsonl")
    swe.write_predictions(rows, predictions_path, AGENT_MODEL_NAME)
    run_id = f"mini-coding-agent-{arm}-{os.path.basename(out_dir)}"
    logs_dir = swe.run_evaluation(predictions_path, dataset_name, run_id)
    return swe.parse_reports(logs_dir, AGENT_MODEL_NAME)


def merge_reports(rows: list[dict], reports: dict[str, dict]) -> list[dict]:
    """把 judge 侧的 resolved 合进 Agent 侧的行，同步算出 verified_pass / false_done。"""
    merged = []
    for row in rows:
        judge = reports.get(row["instance_id"], {})
        resolved = bool(judge.get("resolved", False))
        merged.append({
            **{k: v for k, v in row.items() if k not in ("patch", "workspace")},
            "resolved": resolved,
            "patch_applied": judge.get("patch_applied", False),
            "patch_empty": not bool(row["patch"].strip()),
            "verified_pass": resolved,
            "false_done": row["claimed_done"] and not resolved,
            "blocked": not row["claimed_done"],
        })
    return merged


def summarize(rows: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for arm in ("baseline", "defense"):
        sub = [r for r in rows if r["arm"] == arm and r.get("status") != "error"]
        n = len(sub) or 1
        out[arm] = {
            "runs": len(sub),
            "resolved": sum(r.get("resolved", False) for r in sub),
            "claimed_done": sum(r["claimed_done"] for r in sub),
            "false_done": sum(r.get("false_done", False) for r in sub),
            "blocked": sum(r.get("blocked", False) for r in sub),
            "patch_empty": sum(r.get("patch_empty", False) for r in sub),
            "avg_turns": round(sum(r["turns"] for r in sub) / n, 1),
            "avg_rejections": round(sum(r["rejections"] for r in sub) / n, 2),
        }
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=5, help="从数据集里挑 N 条简单题")
    parser.add_argument("--instances", type=str, default="",
                        help="逗号分隔的 instance_id；给了就精确加载，忽略 --n")
    parser.add_argument("--runs", type=int, default=1, help="每题每臂重复次数")
    parser.add_argument("--dataset", type=str, default=swe.DEFAULT_DATASET)
    parser.add_argument("--arms", type=str, default="baseline,defense")
    parser.add_argument("--skip-judge", action="store_true",
                        help="只跑 Agent 侧、抽 patch，不调用 Docker judge（连通性自检）")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("缺少 OPENAI_API_KEY；示例：\n"
              "  export OPENAI_BASE_URL=... OPENAI_API_KEY=... HARNESS_MODEL=glm-4-flash")
        sys.exit(1)

    ids = [i.strip() for i in args.instances.split(",") if i.strip()]
    instances = swe.load_instances(n=args.n, dataset_name=args.dataset,
                                    instance_ids=ids or None)
    print(f"选中 {len(instances)} 条实例：")
    for inst in instances:
        print(f"  - {inst['instance_id']:<50} {inst['repo']}")

    model = os.environ.get("HARNESS_MODEL", "glm-4-flash")
    model_client = OpenAICompatClient(model=model)

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "runs-swebench", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(root, exist_ok=True)

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    rows_by_arm: dict[str, list[dict]] = {arm: [] for arm in arms}

    for instance in instances:
        for arm in arms:
            for run_idx in range(args.runs):
                print(f"\n>>> {instance['instance_id']} / {arm} / #{run_idx}")
                try:
                    row = run_one(instance, arm, run_idx, model_client, root)
                except Exception as exc:  # 单条失败不应丢弃全批
                    row = {
                        "instance_id": instance["instance_id"], "repo": instance["repo"],
                        "arm": arm, "run": run_idx, "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "claimed_done": False, "rejections": 0, "turns": 0,
                        "tool_calls": 0, "seconds": 0, "patch": "", "workspace": "",
                    }
                    print(f"  [跳过] {type(exc).__name__}: {exc}")
                rows_by_arm[arm].append(row)

    if args.skip_judge:
        merged = []
        for arm, rows in rows_by_arm.items():
            for row in rows:
                merged.append({
                    **{k: v for k, v in row.items() if k not in ("patch", "workspace")},
                    "resolved": None, "patch_applied": None,
                    "patch_empty": not bool(row.get("patch", "").strip()),
                    "verified_pass": None, "false_done": None, "blocked": None,
                })
    else:
        merged = []
        for arm, rows in rows_by_arm.items():
            reports = evaluate_arm(rows, root, arm, args.dataset)
            merged.extend(merge_reports(rows, reports))

    summary = summarize([r for r in merged if r.get("status") != "error"])
    results_path = os.path.join(root, "results.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump({"rows": merged, "summary": summary,
                   "dataset": args.dataset, "model": model,
                   "skip_judge": args.skip_judge},
                  f, ensure_ascii=False, indent=1)

    print("\n===== 汇总 =====")
    header = f"{'指标':<22}{'baseline':<15}{'defense':<15}"
    print(header)
    for key, label in [("runs", "运行次数"), ("resolved", "resolved(SWE-bench)"),
                       ("claimed_done", "宣称完成"), ("false_done", "烂尾进入交付"),
                       ("blocked", "防线拦截"), ("patch_empty", "空 patch"),
                       ("avg_rejections", "平均返工"), ("avg_turns", "平均轮次")]:
        b = summary.get("baseline", {}).get(key, "-")
        d = summary.get("defense", {}).get(key, "-")
        print(f"{label:<22}{str(b):<15}{str(d):<15}")
    if args.skip_judge:
        print("\n[skip-judge] 未调用 Docker judge，resolved/false_done 为 null；"
              "复跑去掉 --skip-judge 即可打分。")
    print(f"\n结果：{results_path}")


if __name__ == "__main__":
    main()
