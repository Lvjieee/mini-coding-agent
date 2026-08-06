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

同一批任务，三个实验臂各跑 N 次，构成**消融阶梯**——每一臂只增加一层、不移除任何层，
这样差异才能归因到具体某一层：

| 实验臂 | pre_done 传感器 | 验收清单 | 独立 Evaluator | 完成判定方式 |
| --- | :---: | :---: | :---: | --- |
| `baseline` | — | — | — | 朴素循环：模型一停止调用工具即视为"完成" |
| `sensors` | ✅ | — | — | 只加确定性检查：可见测试不过就打回 |
| `defense` | ✅ | ✅ | ✅ | 完整防线：三关依次过，未达标打回重做（上限 3 次） |

**为什么必须有中间臂**：没有 `sensors` 臂，就无法回答「是不是只要让 Agent 跑一遍测试就够了，
清单和独立验收是多余的」。这是对本项目核心设计最直接的质疑，只有两臂对比答不了。
加上中间臂后，收益可以拆成两段：**确定性检查贡献多少**、**清单 + 独立验收额外贡献多少**。

实现上有个坑值得记录：`client` 总会传给 `CompletionDefense`（自研循环需要它跑子代理），
所以"不传 evaluator_client"并不能真正关掉验收器——必须用显式开关 `use_evaluator=False`，
否则 `sensors` 臂会悄悄用主模型做验收，消融就失效了。这一点有单测覆盖
（[`../tests/test_eval_arms.py`](../tests/test_eval_arms.py)）。

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

# 3) 只跑部分臂（省预算；例如只对比中间臂与完整防线）
python eval/run_eval.py --runs 3 --arms sensors,defense
```

结果实时输出到终端，并保存为 `eval/runs/<时间戳>/results.json`。

## 实验结果

### 主结果：glm-4-flash × 9 任务 × 3 次（每臂 27 次运行）

选用较弱模型让"第一版翻车"成为常态，从而充分放大两臂差异：

> **注意**：下表是 `baseline` 与 `defense` 的两臂结果。`sensors` 中间臂是后加的，
> **尚未用真实模型跑过**，目前只通过了管线自检与配置单测。因此"确定性检查贡献多少 /
> 清单与独立验收额外贡献多少"这个拆解**还没有数据**，不要在任何对外材料里假设它的结论。
> 补齐命令：`python eval/run_eval.py --runs 3 --arms sensors`（沿用同模型同任务集即可与下表对齐）。

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
- **`sensors` 中间臂尚未用真实模型跑过**：因此现有 `baseline → defense` 的收益还没有被拆分到
  「确定性检查」与「清单 + 独立验收」两层上。在补齐之前，不能声称后两层的独立贡献；
- `defense` 的可见检查只覆盖一条基本断言，防线收益中有多少来自它、多少来自清单与独立验收，
  正是中间臂要回答的问题；
- `PASS_TO_PASS` 对纯 stub 任务检查的是 API 契约，不是完整业务回归；要更接近 SWE-bench，还需要真实仓库、原有测试集和 `FAIL_TO_PASS` / `PASS_TO_PASS` 标注；
- 主结果的 `PASS_TO_PASS` 是对已保存的 54 个历史工作区离线补算，没有重新调用模型；
- 独立验收器存在误拒（见上文"验收器偏严"），当前轮次开销偏高、有优化空间；
- 写简历只使用真实模型跑出的数字，并注明任务集规模与所用模型。

---

## 附：SWE-bench 兼容子集（可选）

上述 9 题是自造的函数级任务，与真实工程差距明显。为了让「机制」和「玩具题目」两个批评分开，
仓库另附一条 A/B 通路，在 SWE-bench 的一个小子集（默认 5 题）上跑同一 Agent，
配合官方 judge 打分。取向明确：**不冲绝对分数**，只证明 harness 兼容 SWE-bench 判卷协议、
并在真实公开 benchmark 上让 `false_done` 差异重现。

**为什么单独摆一节**：判卷需要外部资源（云端配额或本地 Docker + 每实例 1–3GB 镜像），
远重于上面的 9 题主评测；免费弱模型（如 glm-4-flash）在 SWE-bench 上预期
`resolved` 接近 0，把它和主结果混在一起会掩盖主评测的信噪比。

**代码位置**：

- 适配器：[`swebench_adapter.py`](swebench_adapter.py) — 数据集加载/挑题/工作区/patch/两种 judge 后端
- A/B runner：[`run_swebench_ab.py`](run_swebench_ab.py) — 装配 AgentLoop、抽 patch、合并 judge 报告
- 单测：[`../tests/test_swebench_adapter.py`](../tests/test_swebench_adapter.py) — patch 抽取、两种 predictions 格式、挑题启发式、两种 report 解析

### 两条打分路线

| 路线 | 依赖 | 适用 |
| --- | --- | --- |
| `--judge sbcli`（默认） | 免费 API key（邮箱验证），**无需 Docker** | 大多数情况，尤其 Apple Silicon |
| `--judge local` | Docker + `swebench` 包 + 数十 GB 磁盘 | 有 x86 机器且想完全离线 |

官方对本地 Docker 评测的建议配置是 x86_64、≥120GB 磁盘、≥16GB 内存；
ARM（Apple Silicon）标注为实验性，需要本地构建镜像（`--namespace ''`），耗时明显更长。
因此默认走云端。

### 配额是硬约束

`sb-cli get-quotas` 的额度大致是：

- `swe-bench_verified` / `test`：**1 次**
- `swe-bench_lite` / `dev`：约 976 次
- `swe-bench-m` / `dev`：约 997 次

A/B 需要两次提交（baseline + defense），所以**默认数据集用 `SWE-bench_Lite`**（映射到 lite/dev），
把 verified/test 的唯一额度留到最后确认时再用。

### 运行

一次性准备（注意用 `python3 -m pip`，系统上没有裸 `pip` 命令）：

```bash
python3 -m pip install --user swebench datasets sb-cli
sb-cli gen-api-key your@email.com
export SWEBENCH_API_KEY=你收到的key
sb-cli verify-api-key 邮件里的验证码
sb-cli get-quotas
```

跑评测：

```bash
export OPENAI_BASE_URL=... OPENAI_API_KEY=... HARNESS_MODEL=glm-4-flash

python eval/run_swebench_ab.py --n 3 --skip-judge      # 只跑 Agent 侧，验证链路，免费
python eval/run_swebench_ab.py --n 5 --runs 1          # 完整 A/B，云端打分
python eval/run_swebench_ab.py --n 5 --judge local     # 本地 Docker 打分（需自行装 Docker）
```

**取舍与实现说明**：

- 挑题启发式：优先单文件、短 patch、非重仓库（Django/matplotlib/scipy 等默认排除）；
  也可用 `--instances` 精确指定。
- 工作区：`git init` + `fetch --depth 1 <sha>` 浅拉取，只下载目标 commit 的树，避免拉全历史。
- Patch 抽取：`git diff HEAD` + 未跟踪文件登记；抽 patch 不改 HEAD、不留额外 commit。
- Predictions 格式两种：本地 harness 要 JSONL，sb-cli 要以 instance_id 为 key 的 JSON。
- 防线信号：SWE-bench 场景下用 `pytest -x -q --tb=no` 跑仓库已有测试作 `pre_done` 传感器，
  近似「不引入回归」；不启用 Planner 与独立 Evaluator（issue 已是明确目标；judge 才是最终裁判）。

**评测的诚实边界**：

- 免费弱模型上 `resolved` 预期接近 0——本子集不打算竞争绝对分数；
- 单臂 5 题、样本极小，任何结论都是**趋势观测**而非统计结论；
- 云端报告只给 id 级别的 resolved/unresolved/error，不含逐测试明细（本地 judge 才有）；
- SWE-bench 自身在 2026 年被审计出约 27.6% Verified 题存在测试设计缺陷（narrow / brittle tests），
  本地跑分应结合 [OpenAI 2026-02 audit](https://openai.com/index/introducing-swe-bench-verified/) 一起看，不做绝对判据。
