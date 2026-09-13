# GPU 计量实验报告（stage-1，骨架）

> **声明：本报告已于 2026-09-11 按真实实验填写（环境：WSL2 Ubuntu 26.04 + RTX 4060 Laptop 8GB CC8.9 + 驱动 616.56；原始输出见附录 A 与 `experiments/results/20260911T113057Z/`）。结论逐项标注「实测通过 / 未覆盖 / 受阻」；硬件性能计数在本机实测为受阻（ERR_NVGPUCTRPERM），相关能力一律未宣称通过，未以理论 FLOPs、运行时长或公式估算替代硬件实测（spec §7.1、§7.3 第 7 条）。**

对应 issue #13 验收标准第 2 条与 spec §7.3 的 7 项输出要求。章节结构与之一一对应。

---

## 1. 环境与计量权限（§7.3 第 1 项：GPU 型号、驱动、系统、执行环境及可用计量权限）

以下数值来自 2026-09-11 真实实验探测（`worker/depin_worker/gpu_probe.py` + experiments a 实验），非假定值；torch 行更新为实测：**torch 2.14.0+cu130（清华镜像 PyPI CUDA 变体），cuda.is_available()=True**。

| 项 | 值 | 状态 |
| --- | --- | --- |
| GPU 型号 | NVIDIA GeForce RTX 4060 Laptop GPU，显存 8 GB | 已探测 |
| Compute Capability | 8.9（Ada Lovelace） | 已探测 |
| 驱动版本 | 616.56（WSL 下由 Windows 宿主提供，NVML 上报） | 已探测 |
| 操作系统 | WSL2 Ubuntu 26.04，内核 6.18.33.2-microsoft-standard-WSL2 | 已探测 |
| 系统 Python | 3.14.4 | 已探测 |
| pynvml | 可用；NVML UUID：`GPU-81dc8519-806a-0d83-324d-3b0e235ceac9` | 已探测 |
| 依赖管理 | uv 0.12.11；worker 仅依赖 pynvml，torch 等重依赖只装在 experiments/ | 已探测 |
| torch | 经清华镜像安装中（尚未完成，完成后更新本行并注明版本） | 进行中 |
| CUDA Toolkit 版本 / nvcc | 未单独安装（torch 2.14.0+cu130 自带 CUDA 运行时库；nvcc 不在实验路径上） | 已探测 |
| ncu（Nsight Compute） | 2025.1.1.0（build 35528883），`~/opt/nsight-compute/ncu`（redist archive 用户级安装，免 sudo） | 已探测 |
| ncu 计数权限 | **受阻**：真实 profile 报 `ERR_NVGPUCTRPERM - The user does not have permission to access NVIDIA GPU Performance Counters on the target device 0`；WSL 内核驱动参数由 Windows 宿主管理，无法在 WSL 内常规放行 | 实测受阻 |
| NVML 逐字段可用性 | name/uuid/driver/memory.total/compute_cap 可读；每进程计算占用 API 受限；功耗/温度等字段见 a_gpu_probe.json 逐条记录（不可用字段记 unavailable+原始错误） | 已探测（详见 a_gpu_probe.json） |
| nvidia-smi 交叉验证 | `/usr/lib/wsl/lib/nvidia-smi` 可用，--query-gpu/--query-compute-apps 均正常返回 | 已探测 |

已知环境约束（影响后续所有结论的解释，不构成实测结论）：WSL2 下 NVML 部分字段（功耗、温度、每进程查询）常见缺失，Windows 侧进程对 WSL 内 NVML 不可见（归因盲区，见 trust-assumptions.md §1.2）——本机实际可用性以 `experiments/results/20260911T113057Z/a_gpu_probe.json` 的逐字段探测输出为准。

## 2. 工具与原始指标（§7.3 第 2 项：硬件计数工具、原始指标、单位、覆盖范围和转换规则）

每种工具记录：所用版本、采集命令、原始输出文件路径、单位与转换规则。数据来源差异见 trust-assumptions.md §2.1。

### 2.1 ncu（硬件计数器——唯一拟议的计费口径来源，可用性待实测）

| 记录项 | 内容 |
| --- | --- |
| 使用版本与调用方式 | ncu 2025.1.1.0（`ncu --csv --metrics ... <负载>`）；**计数权限受阻，未取得任何硬件计数值** |
| 选定 metric 清单及单位 | 计划：sm__inst_executed_pipe_fp32/fp64/alu、tensor pipe 系列；实际：未采到（权限受阻），`--query-metrics` 可用但 profile 被拒 |
| 覆盖范围 | 未获得（受阻） |
| 原始→计费转换规则 | 未决（O1–O4）；因计数未采到，本报告无可转换原始值 |
| 原始输出文件 | c_counter_collection.json（status=blocked 及原因） |

### 2.2 NVML（驱动自报——时间线与可用性佐证，不作计费值）

| 记录项 | 内容 |
| --- | --- |
| 采集方式 | nvml_sampler.py 后台线程 JSONL；f 实验使用 0.1s 与 1s 两档各 5 轮 |
| 可用字段 | 时间戳（epoch）、整机 GPU 利用率/显存（% / MiB）；每进程利用率/显存字段在本机不可用（记 null）；进程退出信号可用 |
| 原始 JSONL | f_samples_0.1s_rep0-4.jsonl、f_samples_1s_rep0-4.jsonl |

### 2.3 torch.profiler（shape 推导——仅作交叉核对，不得标为实测）

| 记录项 | 内容 |
| --- | --- |
| 算子清单与推导 FLOPs（**推导值，非实测**） | c_torch_profiler_estimate.json（torch.profiler with_flops）；负载算子清单见 b_workload_manifest.json |
| 与 ncu 实测值比对 | **不可比对**（ncu 受阻，无实测值；推导值不得冒充实测） |

## 3. 精度区分、算子覆盖与订单归因（§7.3 第 3 项）

- 实验负载：合成数据 + 小 CNN（训练 + 批量推理；decision-log.md B4——实验负载，非已批准模板）。
- 硬件计数区分精度：**受阻**（ERR_NVGPUCTRPERM）。负载侧可按 autocast 配置区分 fp32/fp16/bf16 三档真实执行（b 实验三档 loss 均下降），但这是执行配置，不是硬件计数证据。
- Tensor Core 与普通算子区分：**受阻**（同上，需要 ncu tensor pipe 指标）。
- 归因方式结论：**进程级归因实测通过**——nvidia-smi --query-compute-apps 可把任务负载与无关干扰进程（d 实验）分开（各自的 PID/显存/进程名）；NVML 每进程 API 不可用；ncu attach 受阻。整机独占窗口归因不依赖被拒能力，但未单独实验（未覆盖）。
- 多卡场景归因：未覆盖（本机单卡）。

## 4. 测量开销与重放（§7.3 第 4 项）

- 计量开销：ncu 开销**未测得**（权限受阻）。NVML 采样开销低（JSONL 落盘，0.1s 档运行正常）。
- kernel replay 依赖：未测得（e 实验受阻，status=blocked 留档）。
- 整体重放：未测得（同上）。
- 采样盲区实测：停止延迟实验 f——0.1s 档观测到的停止延迟 ~0.10s 量级、1s 档 ~1.00s 量级（受采样间隔下限约束；每进程字段缺失时以进程退出信号为准，多轮原始 JSONL 留档）。这直接约束 undecided-parameters.md P1/P2（预算保护安全余量不得小于采样间隔+停止延迟）。

## 5. 污染测试（§7.3 第 5 项：同机其他任务、空操作、重复执行、伪造报告）

| 用例 | 设计 | 结果 |
| --- | --- | --- |
| 同机无关 GPU 任务 | d 实验：任务运行同时起独立 matmul 干扰进程 | **实测通过（进程级）**：query-compute-apps 两个进程各自可见可分；整机利用率口径无法拆分（每进程字段缺失） |
| 空操作对照 | f 实验的噪声负载之外未设独立空操作计费用例 | 未覆盖（计数受阻后该用例依赖硬件计数，暂无意义；保护在负载真实性与 shape 估算侧） |
| 重复执行 | 同一配置多轮重复（f 实验 5 轮/档）采样时间线可区分每次执行 | 部分通过（时间线可区分；计数偏差未测——硬件计数受阻） |
| 伪造报告 | 未设计独立用例 | 未覆盖（stage-1 链上只有 scoreReportHash 占位；见 trust-assumptions.md §2 的理论边界） |
| Windows 宿主侧负载 | 未执行 Windows 侧干扰实验 | 未覆盖（trust-assumptions.md 已声明该盲区；量化待原生环境或后续补测） |

## 6. 核验证据与信任剩余（§7.3 第 6 项：核验组织实际取得什么证据、如何检查、哪些仍需信任设备所有者）

证据能力边界与剩余信任的完整陈述见 `trust-assumptions.md` §2（三句话结论：三份对同一自报文件的签名不证明文件真实；哈希一致不证明计算正确；重跑测的是重跑那次）。本节只填实验侧结论：

- 受控重跑差异区间：未测得（硬件计数受阻）。
- NVML 时间线一致性：实测通过——f 实验采样时间线能对上每轮负载的启动/停止（进程退出信号 + 整机利用率变化，原始 JSONL 可核）。
- 可独立复现：GPU 型号/驱动/显存/UUID 探测输出、nvidia-smi 进程清单、NVML 整机利用率时间线、负载配置与 seed（可重跑对照 loss 轨迹）。
- 不可独立复现（必须信任设备方或受阻）：硬件运算计数（本机不可得）、每进程利用率、原始执行的运算量自报值。

## 7. 逐项结论表（§7.3 第 7 项：每项能力给出「实测通过 / 未覆盖 / 受阻」，不能只列工具名称）

| # | 能力项 | 结论（实测通过/未覆盖/受阻） | 证据（对应章节与文件） |
| --- | --- | --- | --- |
| 1 | NVML 环境探测与逐字段可用性 | 实测通过（每进程字段除外） | §1、§2.2 |
| 2 | ncu 可用性与计数权限（WSL2 实测） | 受阻（ERR_NVGPUCTRPERM，原始错误留档） | §1、§2.1 |
| 3 | 按精度运算计数（各精度逐一） | 受阻（硬件计数不可得；负载侧三精度真实执行已验证） | §3 |
| 4 | Tensor Core 与普通算子区分 | 受阻 | §3 |
| 5 | 任务级归因（独占窗 / per-process / attach） | 部分通过：per-process（smi）实测通过；attach 受阻；独占窗未单独实验 | §3 |
| 6 | 同机污染隔离 | 部分通过（进程级可分；整机利用率口径不可拆） | §5 |
| 7 | 空操作不产生可计费计数 | 未覆盖 | §5 |
| 8 | 测量开销与 replay 影响 | 受阻（e 实验未执行，envelope 留档） | §4 |
| 9 | 停止延迟与预算预警可行性 | 实测通过（0.1s/1s 两档多轮，原始 JSONL；仅进程退出信号可用） | §4 |
| 10 | 独立核验证据可复制性 | 部分通过（时间线/环境/负载可复现；计数证据不可复现） | §6 |
| 11 | 多卡与多提供者归因 | 【未覆盖（本机单卡，如实保留为未覆盖项）】 | — |

## 8. 缺口与替代方案（若有）

spec §7.3：如果实际设备不能满足硬件计量或独立核验证据要求，提交具体缺口、最小替代方案及其改变的信任假设，等待业务确认；不得将有缺口的方案称为已达到真实结算要求。

- **缺口 1：硬件性能计数不可得（本机 WSL2 实测受阻）** → 最小替代方案候选：(a) Windows 宿主管理员修改 `NVreg_RestrictProfilingToAdminUsers=0` 后复测（WSL 是否遵循待实测）；(b) Windows 侧安装 Nsight Compute 对 WSL target 采集；(c) 换原生 Linux GPU 机器采集。信任假设变化：任一方案落地前，**任何按运算量计费的口径都不可宣称可信**（M1/M2/R2 待确认）。
- **缺口 2：每进程利用率/显存 NVML 字段缺失** → 替代：进程级归因用 query-compute-apps（进程清单）+ 整机利用率时间线交叉；不能支撑共享 GPU 的精确分摊（M2 候选 b 受限）。
- **缺口 3：重放开销未测** → 在计数权限解决后补 e 实验；在此之前计费口径不得含"重放执行"部分（O5/O6 未决）。
- 以上缺口在用户确认前，stage-1 不宣称"已具备真实可信收费能力"（issue #13 验收标准 5）。
- **【2026-09-13 后记，D1】**用户已确认计费口径改为「模板工作单元」（训练 step / 推理 batch·token·样本数，结果可复算核验）——上述硬件计数缺口**不再阻塞计费**，本报告的计量实验降级为执行真实性研究；计费信任假设见 trust-assumptions.md §2 与 decision-log.md D1。

---

## 附录 A：原始数据与脚本清单

- 实验入口：`experiments/run_experiment.py`（`--only env|workload|attribution|pollution|overhead`，见 acceptance-tests.md T3/T4）。
- 原始输出（采集 2026-09-11，命令 `uv run python run_experiment.py [--only a,b,...]`，目录 `experiments/results/20260911T113057Z/`）：
  - `a_gpu_probe.json` / `a_environment_survey.json` / `a_environment.json`——环境与 ncu 权限探测（含 ERR_NVGPUCTRPERM 证据）
  - `b_workload_manifest.json` / `b_workload_results.json` / `b_workload_train_infer.json`——三精度训练+推理（loss 轨迹：fp32 2.321→1.869、fp16 2.321→2.253、bf16 2.320→1.822）
  - `c_counter_collection.json`（受阻）/ `c_torch_profiler_estimate.json`（**推导估算，非实测**）/ `c_op_manifest.json`
  - `d_attribution_test.json` / `d_noise_output.txt` / `d_workload_output.txt`——进程归因
  - `e_replay_overhead.json`（受阻）
  - `f_stop_latency.json` + `f_samples_*.jsonl`（10 份采样）+ `f_noise_*.txt`——停止延迟
  - `summary.json` / `g_evidence_report.json` / `REPORT.md`——汇总
- 复现步骤（wsl 机器，uv 环境）：`cd ~/depin/experiments && uv run --project ~/depin python run_experiment.py`（全量 a–g）；ncu 需 `export PATH="$HOME/opt/nsight-compute:$PATH"`。

## 附录 B：与 issue #13 验收标准的对应

- 验收标准 2（真实 GPU 实验附原始指标、单位、环境、脚本、误差/覆盖范围和独立验证方法）→ 本报告 §1–§7 + 附录 A（2026-09-11 已按实测填写；受阻项如实标注，不以估算冒充）。
- 计量相关未决参数（M1–M4、O1–O6、P1–P2）→ 本报告结论填完后按 §8 汇总提请确认，清单见 `undecided-parameters.md`。
