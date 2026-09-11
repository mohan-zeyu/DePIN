# GPU 计量可行性实验（stage-1）

对应 GitHub issue #13「搭建最小系统，验证 GPU 计量」的实验部分，
以及 spec（docs/project-spec.md）§7.3「必须先做的技术验证」七项要求。

目标：在真实 NVIDIA GPU（RTX 4060 Laptop / WSL2）上，用**真实 AI 负载**
（torch 小 CNN，确定性合成数据，禁止 sleep/空循环冒充）产出原始证据，
逐项给出「实测通过／未覆盖／受阻」三态结论。

## 实验清单与 spec §7.3 对应关系

| 字母 | 名称 | 目的 | spec §7.3 | 主要产物 |
| --- | --- | --- | --- | --- |
| a | environment_survey | GPU 型号/驱动/系统/CUDA 运行时/torch 版本、ncu 发现（PATH/venv bin/pip/常见路径）、**计数权限实测**（ERR_NVGPUCTRPERM 检测） | 第 1 项 | `a_environment.json`、`a_gpu_probe.json` |
| b | workload_train_infer | 2 conv + 1 fc 小 CNN（3x32x32，合成数据固定 seed）；训练 N 步 + 批量推理 M 批；fp32 / fp16 autocast / bf16（支持时）；记录 op/shape 清单 | 第 1/2 项前置 | `b_workload_manifest.json`、`b_workload_results.json` |
| c | counter_collection | `ncu --query-metrics` 探测指标（fp64/fp32/alu pipe、hmma/imma tensor 指标，**实际名称以 query 结果为准、原样记录**）；ncu 采样原始 CSV；torch.profiler with_flops 一份，**明确标注为 shape 推导估算而非硬件实测**（spec §7.1） | 第 2/3 项 | `c_ncu_query_metrics.txt`、`c_ncu_profile_*.csv`、`c_torch_profiler_estimate.json` |
| d | attribution_test | 负载运行时并发一个独立 matmul 干扰进程：nvidia-smi --query-compute-apps、NVML 每进程 API 能否分开两者；ncu 是否只统计目标进程（CSV pid 检验） | 第 3/5 项 | `d_attribution.json`、`d_ncu_target_only.csv` |
| e | replay_overhead | kernel replay vs application replay vs 无 ncu 基线：墙钟开销倍数、两种模式计数差异 | 第 4 项 | `e_replay_overhead.json`、`e_ncu_*_replay.csv` |
| f | stop_latency | 采样间隔 100ms/1s 两档，已知时刻请求停止，测「停止请求 → 采样器观察到利用率归零/进程退出」延迟分布（多次 min/median/max） | 第 4 项 / §8.2 预算保护 | `f_stop_latency.json`、`f_samples_*.jsonl` |
| g | evidence_report | 汇总 a-f → `REPORT.md`（七项三态结论 + 信任假设清单），供整理进 `docs/stage1/gpu-metering-report.md` | 第 6/7 项 | `REPORT.md`、`g_evidence_report.json` |

三态口径：**实测通过**＝有真实运行证据；**未覆盖**＝没测；**受阻**＝实测后
发现环境/权限不满足。依赖缺失（无 torch/ncu/权限）时实验记录「受阻」并
保留原始错误，绝不假装成功。

## 安装（WSL 目标机）

```bash
bash experiments/setup_wsl.sh
```

- 系统 Python 3.14 缺 venv/ensurepip → 用 uv 项目模式（`pyproject.toml` +
  `uv.lock` + `.venv`），默认 `PYTHON_VERSION=3.12`；
- 镜像默认清华 `https://pypi.tuna.tsinghua.edu.cn/simple`（`UV_DEFAULT_INDEX` 可覆盖）；
- `nvidia-nsight-compute`（ncu wheel）装不上会**如实失败**（脚本退出码 1），
  c/e/d 的 ncu 部分届时记「受阻」。

依赖说明见 `requirements.txt`。

## 运行

```bash
cd experiments
uv run python run_experiment.py                  # 全部 a-g
uv run python run_experiment.py --only a         # 只跑环境调查
uv run python run_experiment.py --only a,b,c     # 子集（字母/全名/b1 别名）
uv run python run_experiment.py --list           # 列出实验
uv run python run_experiment.py --train-steps 50 --stop-reps 3   # 缩小规模
```

- 产物写入 `experiments/results/<UTC 时间戳>/`（run_meta.json、各实验
  envelope JSON、原始 CSV/TXT/JSONL、REPORT.md、summary.json）；
- **GPU 门禁**：无可用 NVIDIA GPU 时直接退出码 2 并打印检查细节，
  不允许静默通过。`--allow-no-gpu` 仅用于环境诊断，实验仍会如实记「受阻」；
- 每个实验的 envelope 统一结构：`status / reason / started_at /
  duration_seconds / artifacts / data`。

## 预期在 RTX 4060 WSL2 上的已知风险（写代码时已按“探测而非假设”处理）

- nvidia-smi 在 `/usr/lib/wsl/lib/nvidia-smi`（非交互 PATH 没有）→ 自动发现；
- NVML power/temp 可能 N/A、每进程 compute 查询可能受限 → 逐字段记录；
- ncu 对 WSL 的支持要实测：pip wheel 是否可用、`ERR_NVGPUCTRPERM` 权限
  （WSL 内核驱动参数由 Windows 管理，无法常规放开）→ a/c 实验实测并记录；
- DCGM 在 WSL 通常不可用 → 未使用。

## 目录结构

```
experiments/
  run_experiment.py          # 编排 CLI（参数校验、GPU 门禁、envelope 汇总）
  setup_wsl.sh               # uv 项目模式安装脚本
  requirements.txt           # 依赖清单（含安装说明）
  depin_experiments/
    common.py                # 状态常量/统计/子进程/ResultContext/GPU 门禁/选项
    env_survey.py            # 实验 a
    workloads.py             # 实验 b + 模型/训练/推理/profiler 估算
    workload_entry.py        # 负载子进程入口（被 ncu 采样）
    matmul_noise.py          # 独立 matmul 干扰进程
    ncu_runner.py            # ncu 发现/调用/输出解析（纯函数可单测）
    counter_collection.py    # 实验 c
    attribution.py           # 实验 d
    replay.py                # 实验 e
    stop_latency.py          # 实验 f（复用 worker 的 NvmlSampler）
    report.py                # 实验 g（三态聚合/信任假设/渲染，纯函数可单测）
  tests/                     # 纯逻辑测试（不依赖 GPU/torch/ncu）
    fixtures/                # ncu/nvidia-smi 输出样例
  results/                   # 运行产物（按时间戳分目录，不入库）
```

## 测试（本机无 GPU 也可跑）

```bash
cd experiments
python3 -m unittest discover -s tests -p "test_*.py"
# 或（有 pytest 时，从仓库根目录）
python3 -m pytest worker/tests experiments/tests
```

覆盖：ncu --query-metrics / profile CSV / nvidia-smi compute-apps 解析器
（样例 fixture）、kernel 分类启发式、报告三态聚合与信任假设规则、markdown
渲染、停止延迟纯函数、重放开销纯函数、CLI 参数校验（--only 解析、精度/
间隔/正整数校验、GPU 门禁不崩溃）、op/shape 清单一致性。

## 诚实声明

- torch.profiler 的 FLOPs 是 shape 推导**估算**，产物中明确标注，
  不得作为计费口径（spec §7.1）；
- 重放（kernel/application replay）引入的额外执行不属于业务执行，
  计费边界未定（spec §7.3-4 / §12），本实验只量化不裁决；
- 计量材料由 Worker 侧生成，签名/哈希/重放一致性不能证明原始执行的物理
  真实性——这些信任边界写进每份 REPORT.md 的信任假设一节；
- 单机单卡已覆盖；多卡/多提供者归因、跨集群执行为「未覆盖」。
