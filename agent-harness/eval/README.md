# 完成防线 A/B 评测

> 用一个可复现的对照实验回答：**Agent 的"完成防线"到底值不值得？**
> 防线 = 行为级验收清单 + pre_done 传感器 + 独立 Evaluator + 打回重注入（Ralph-Loop）。
> 裁判拆成 `hidden tests`（目标行为）和 `PASS_TO_PASS`（回归/API 契约），最终真完成要求两者都通过。

**TL;DR** — 弱模型 + 9 个编码任务、每臂 27 次运行：防线把**烂尾交付率从 77.8% 压到 0%**，
同时把**真完成数从 5 提到 9**（返工救回），代价是对话轮次约 **3.3×**。补充可靠性指标：
`pass^3` 从 **1/9** 提到 **2/9**；由于样本只有 27 次运行，区间与任务级指标一并报告，
不把一次实验写成模型能力的普遍结论。
强模型 + 简单任务下出现天花板效应，双臂无差异——评测的有效边界也一并量化。

---

## 实验设计

同一批任务，两个实验臂各跑 N 次，唯一变量是"是否开启完成防线"：

| 实验臂 | 完成判定方式 |
| --- | --- |
| `baseline` | 朴素循环：模型一停止调用工具即视为"完成"，无清单、无传感器、无独立验收 |
| `defense` | 完整 harness：宣称完成时依次过 pre_done 传感器（可见检查）→ 清单核对 → 独立 Evaluator，未达标打回重做（上限 3 次） |

**任务集**：9 个小型编码任务 = 5 个基础实现/修 bug 类 + 4 个边界陷阱类
（`merge_intervals`、`format_size`、`cart_total` 多文件修 bug、`parse_duration`）。

**判卷对 Agent 完全不可见**：隐藏测试和回归测试都在运行结束后才写入工作区执行，
覆盖空输入、偶数长度、引号转义、闰年、区间相接等边界，避免"实现与测试共享同一误解"。
每次运行使用全新隔离工作区 `eval/runs/<时间戳>/<任务>-<臂>-<序号>/`。

### 两类裁判

- `hidden_test` / `FAIL_TO_PASS`：目标行为测试，验证题目要求是否被实现；测试文件在 Agent 结束后才写入。
- `PASS_TO_PASS`：回归/API 契约测试，验证 Agent 没有删掉公共函数、改变签名，或破坏与目标无关的已有逻辑；
  纯 stub 任务检查 API 契约，多文件已有逻辑任务检查既有行为。每次实验启动前，脚本会把这组测试放在系统临时目录
  执行一次，确认裁判本身有效，但不把它暴露给 Agent；Agent 结束后才把正式裁判写入工作区。

最终 `verified_pass = hidden_pass && pass_to_pass`，而不是只看隐藏测试。

## 指标

| 指标 | 含义 |
| --- | --- |
| `hidden_pass` | 目标行为测试通过 |
| `pass_to_pass` | 回归/API 契约测试通过 |
| `verified_pass` | `hidden_pass` 与 `pass_to_pass` 都通过——**真完成** |
| `false_done` | 宣称完成但任一裁判失败——**烂尾进入交付**，防线要压低的核心指标 |
| `blocked` | 防线判定未达标、以交接文件收尾——烂尾被拦在交付线之前 |
| `pass@1` | 单次运行通过率；报告 Wilson 95% 区间 |
| `pass^k` | 同一任务的 k 次独立运行全部通过，衡量重复可靠性 |
| `avg_rejections` | 平均返工（打回）次数 |
| `avg_turns` | 平均模型调用轮次——防线的**代价侧**指标 |

## 快速开始

```bash
# 1) 管线自检（不调真实模型；仅验证机制，数字不可引用）
python eval/run_eval.py --mock

# 2) 真实评测
export OPENAI_BASE_URL=... OPENAI_API_KEY=... HARNESS_MODEL=...
export HARNESS_EVAL_MODEL=...        # 可选：验收用不同模型
python eval/run_eval.py --runs 3     # 建议 3 次以上以观察方差
```

结果实时输出到终端，并保存为 `eval/runs/<时间戳>/results.json`。

## 实验结果

### 主结果：glm-4-flash × 9 任务 × 3 次（每臂 27 次运行）

选用较弱模型让"第一版翻车"成为常态，从而充分放大两臂差异：

| 指标 | baseline（关防线） | defense（开防线） |
| --- | :---: | :---: |
| 运行次数 | 27 | 27 |
| 宣称完成 | 26 | 1 |
| PASS_TO_PASS 通过 | 23 · 85.2% | 24 · 88.9% |
| **真完成（隐藏 + 回归都通过）** | 5 · 18.5% | **9 · 33.3%** |
| **烂尾进入交付** | **21 · 77.8%** | **0 · 0%** |
| `pass^3`（任务 3 次全过） | 1 / 9 | **2 / 9** |
| 防线拦截 / 未交付 | 1 | 26 |
| 平均返工次数 | 0 | 2.37 |
| 平均模型轮次 | 5.0 | 16.3 · ≈3.3× |

**两条核心观察**

- **烂尾交付显著下降**：baseline 有 21/27（77.8%，Wilson 95% CI **59.2%–89.4%**）
  带着"完成"标签交付却未通过裁判；defense 为 **0/27**（95% CI **0%–12.5%**）。
  这意味着本样本里没有假完成流出交付线，不意味着总体真实概率绝对为零。
- **真完成观察值上升但样本不足以证明显著**：baseline 为 5/27（18.5%，95% CI **8.2%–36.7%**），
  defense 为 9/27（33.3%，95% CI **18.6%–52.2%**），两个区间重叠；应表述为
  "观察到 5 → 9"，不能表述为已经证明普遍提升 80%。任务级 `pass^3` 从 1/9 到 2/9，
  同样只说明本批任务上的重复可靠性观察值。

代价侧诚实标注：平均轮次 5.0 → 16.3（约 3.3×），平均返工 2.37 次——防线用更多算力
换交付质量。

### 一处值得说的观察：验收器偏严

defense 的"真完成"有 9 次，但只有 1 次是干净的 `done` 收尾——其余是代码实际已通过
隐藏测试、Evaluator 却仍在打回，直到耗尽重试/预算才停。这说明**独立验收器偏严、存在
误拒**，是拉高轮次的主因，也是下一步最该优化的点（提高验收器精度 / 降低误拒率）。
把这一点讲出来，比只报漂亮数字更可信。

### 边界：强模型下的天花板效应

在强模型（如 qwen3.5-plus）+ 基础函数级任务上，baseline 本身几乎不翻车，两臂
`false_done` 同为 0，防线无从触发，仅体现为约 2× 的轮次开销。
**评测在"模型能力足以稳定解题"时会失去区分度**——引用数字时务必说明模型与任务难度。

## 已知局限（引用数字时请一并说明）

- 任务集规模有限（9 个函数级任务），结论不能直接外推到大型工程任务；
- `PASS_TO_PASS` 对纯 stub 任务检查的是 API 契约，不是完整业务回归；要更接近 SWE-bench，还需要真实仓库、原有测试集和 `FAIL_TO_PASS` / `PASS_TO_PASS` 标注；
- 主结果的 `PASS_TO_PASS` 是对已保存的 54 个历史工作区离线补算，没有重新调用模型；
- 独立验收器存在误拒（见上文"验收器偏严"），当前轮次开销偏高、有优化空间；
- 写简历只使用真实模型跑出的数字，并注明任务集规模与所用模型。

---

## 附：SWE-bench Verified 兼容子集（可选）

上述 9 题是自造的函数级任务，与真实工程差距明显。为了让「机制」和「玩具题目」两个批评分开，
仓库另附一条 A/B 通路，在 SWE-bench Verified 的一个小子集（默认 5 题）上跑同一 Agent，
配合官方 judge 打分。取向明确：**不冲绝对分数**，只证明 harness 兼容 SWE-bench 判卷协议、
并在真实公开 benchmark 上让 `false_done` 差异重现。

**为什么单独摆一节**：判卷需要 Docker + `swebench` 包 + 每实例 1–3GB 镜像 + 网络拉仓库，
远重于上面的 9 题主评测；免费弱模型（如 glm-4-flash）在 SWE-bench 上预期
`resolved` 接近 0，把它和主结果混在一起会掩盖主评测的信噪比。

**代码位置**：

- 适配器：[`swebench_adapter.py`](swebench_adapter.py) — 数据集加载/挑题/工作区/patch/官方 judge 调用
- A/B runner：[`run_swebench_ab.py`](run_swebench_ab.py) — 装配 AgentLoop、抽 patch、合并 judge 报告
- 单测：[`../tests/test_swebench_adapter.py`](../tests/test_swebench_adapter.py) — patch 抽取、predictions 格式、挑题启发式、report 解析

**运行**：

```bash
pip install swebench datasets
export OPENAI_BASE_URL=... OPENAI_API_KEY=... HARNESS_MODEL=glm-4-flash

python eval/run_swebench_ab.py --n 3 --skip-judge          # 只跑 Agent 侧，验证链路
python eval/run_swebench_ab.py --n 5 --runs 1              # 完整 A/B（含 Docker judge）
python eval/run_swebench_ab.py --instances "sympy__sympy-20590,pytest-dev__pytest-11148"
```

**取舍与实现说明**：

- 挑题启发式：优先单文件、短 patch、非重仓库（Django/matplotlib/scipy 等默认排除）；
  也可用 `--instances` 精确指定。
- 工作区：`git init` + `fetch --depth 1 <sha>` 浅拉取，只下载目标 commit 的树，避免拉全历史。
- Patch 抽取：`git diff HEAD` + 未跟踪文件登记；抽 patch 不改 HEAD、不留额外 commit。
- 防线信号：SWE-bench 场景下用 `pytest -x -q --tb=no` 跑仓库已有测试作 `pre_done` 传感器，
  近似「不引入回归」；不启用 Planner 与独立 Evaluator（issue 已是明确目标；Docker 判卷才是最终裁判）。
- 判卷：`python -m swebench.harness.run_evaluation` 由官方 harness 拉 Docker 镜像；
  产物在 `logs/run_evaluation/<run_id>/mini-coding-agent/<instance_id>/report.json`。

**评测的诚实边界**：

- 免费弱模型上 `resolved` 预期接近 0——本子集不打算竞争绝对分数；
- 单臂 5 题、样本极小，任何结论都是**趋势观测**而非统计结论；
- SWE-bench 自身在 2026 年被审计出约 27.6% Verified 题存在测试设计缺陷（narrow / brittle tests），
  本地跑分应结合 [OpenAI 2026-02 audit](https://openai.com/index/introducing-swe-bench-verified/) 一起看，不做绝对判据。

