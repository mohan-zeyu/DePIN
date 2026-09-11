# GPU 计量可行性实验报告（stage-1 草稿）

- 生成时间：2026-09-11T11:35:48.382834+00:00
- 结果目录：/home/mohan/depin/experiments/results/20260911T113057Z
- 主机：Linux-6.18.33.2-microsoft-standard-WSL2-x86_64-with-glibc2.43
- GPU：NVIDIA GeForce RTX 4060 Laptop GPU / 驱动 616.56 / compute capability 8.9 / WSL=True

> 状态口径：实测通过＝有真实运行证据支撑；未覆盖＝本阶段没测；
> 受阻＝实测后发现环境/权限不满足。禁止把估算标成实测（spec §7.1）。

## 1. spec §7.3 七项要求逐项结论

| # | 要求 | 结论 | 依据实验 |
| --- | --- | --- | --- |
| 1 | GPU 型号、驱动、系统、执行环境及可用计量权限 | 受阻 | a(environment_survey) |
| 2 | 使用的硬件计数工具、原始指标、单位、覆盖范围和转换规则 | 受阻 | c(counter_collection) |
| 3 | 是否能区分所需精度与算子类别，如何归因到任务和参与 GPU | 受阻 | c(counter_collection)、d(attribution_test) |
| 4 | 测量开销，是否需要重放，以及如何区分业务执行与测量引起的额外执行 | 受阻 | e(replay_overhead) |
| 5 | 同机其他任务、空操作、重复执行或伪造报告是否会污染计量 | 受阻 | d(attribution_test)、e(replay_overhead) |
| 6 | 核验组织实际取得什么证据、如何检查、哪些部分仍需信任设备所有者 | 受阻 | a(environment_survey)、c(counter_collection)、d(attribution_test)、e(replay_overhead)、f(stop_latency) |
| 7 | 对每项能力给出“实测通过／未覆盖／受阻” | 实测通过 | g(evidence_report) |

## 2. 各实验明细

### a environment_survey：受阻

- 结论：计数权限未获实测通过（verdict=error）
- 耗时：7.1s
- 产物：
  - gpu_probe: `/home/mohan/depin/experiments/results/20260911T113057Z/a_gpu_probe.json`
  - summary: `/home/mohan/depin/experiments/results/20260911T113057Z/a_environment.json`

### b workload_train_infer：实测通过

- 结论：训练+推理完成（精度 ['bf16', 'fp16', 'fp32']）
- 耗时：7.9s
- 产物：
  - gpu_probe: `/home/mohan/depin/experiments/results/20260911T113057Z/a_gpu_probe.json`
  - manifest: `/home/mohan/depin/experiments/results/20260911T113057Z/b_workload_manifest.json`
  - results: `/home/mohan/depin/experiments/results/20260911T113057Z/b_workload_results.json`
  - summary: `/home/mohan/depin/experiments/results/20260911T113057Z/a_environment.json`

### c counter_collection：受阻

- 结论：计数权限受阻（verdict=error，证据见 counter_permission.evidence）
- 耗时：6.3s
- 产物：
  - gpu_probe: `/home/mohan/depin/experiments/results/20260911T113057Z/a_gpu_probe.json`
  - manifest: `/home/mohan/depin/experiments/results/20260911T113057Z/b_workload_manifest.json`
  - op_manifest: `/home/mohan/depin/experiments/results/20260911T113057Z/c_op_manifest.json`
  - results: `/home/mohan/depin/experiments/results/20260911T113057Z/b_workload_results.json`
  - summary: `/home/mohan/depin/experiments/results/20260911T113057Z/c_counter_collection.json`
  - torch_profiler_estimate: `/home/mohan/depin/experiments/results/20260911T113057Z/c_torch_profiler_estimate.json`

### d attribution_test：实测通过

- 结论：进程级归因可行（smi）；ncu 部分未验证/受阻
- 耗时：15.2s
- 产物：
  - gpu_probe: `/home/mohan/depin/experiments/results/20260911T113057Z/a_gpu_probe.json`
  - manifest: `/home/mohan/depin/experiments/results/20260911T113057Z/b_workload_manifest.json`
  - noise_output: `/home/mohan/depin/experiments/results/20260911T113057Z/d_noise_output.txt`
  - op_manifest: `/home/mohan/depin/experiments/results/20260911T113057Z/c_op_manifest.json`
  - results: `/home/mohan/depin/experiments/results/20260911T113057Z/b_workload_results.json`
  - summary: `/home/mohan/depin/experiments/results/20260911T113057Z/d_attribution.json`
  - torch_profiler_estimate: `/home/mohan/depin/experiments/results/20260911T113057Z/c_torch_profiler_estimate.json`
  - workload_output: `/home/mohan/depin/experiments/results/20260911T113057Z/d_workload_output.txt`

### e replay_overhead：受阻

- 结论：计数权限受阻（error），重放实验无法执行
- 耗时：18.3s
- 产物：
  - gpu_probe: `/home/mohan/depin/experiments/results/20260911T113057Z/a_gpu_probe.json`
  - manifest: `/home/mohan/depin/experiments/results/20260911T113057Z/b_workload_manifest.json`
  - noise_output: `/home/mohan/depin/experiments/results/20260911T113057Z/d_noise_output.txt`
  - op_manifest: `/home/mohan/depin/experiments/results/20260911T113057Z/c_op_manifest.json`
  - results: `/home/mohan/depin/experiments/results/20260911T113057Z/b_workload_results.json`
  - summary: `/home/mohan/depin/experiments/results/20260911T113057Z/e_replay_overhead.json`
  - torch_profiler_estimate: `/home/mohan/depin/experiments/results/20260911T113057Z/c_torch_profiler_estimate.json`
  - workload_output: `/home/mohan/depin/experiments/results/20260911T113057Z/d_workload_output.txt`

### f stop_latency：实测通过

- 结论：每档间隔均完成；仅进程退出信号可用（每进程/利用率字段不可用）
- 耗时：236.0s
- 产物：
  - gpu_probe: `/home/mohan/depin/experiments/results/20260911T113057Z/a_gpu_probe.json`
  - manifest: `/home/mohan/depin/experiments/results/20260911T113057Z/b_workload_manifest.json`
  - noise_0.1s_rep0: `/home/mohan/depin/experiments/results/20260911T113057Z/f_noise_0.1s_rep0.txt`
  - noise_0.1s_rep1: `/home/mohan/depin/experiments/results/20260911T113057Z/f_noise_0.1s_rep1.txt`
  - noise_0.1s_rep2: `/home/mohan/depin/experiments/results/20260911T113057Z/f_noise_0.1s_rep2.txt`
  - noise_0.1s_rep3: `/home/mohan/depin/experiments/results/20260911T113057Z/f_noise_0.1s_rep3.txt`
  - noise_0.1s_rep4: `/home/mohan/depin/experiments/results/20260911T113057Z/f_noise_0.1s_rep4.txt`
  - noise_1s_rep0: `/home/mohan/depin/experiments/results/20260911T113057Z/f_noise_1s_rep0.txt`
  - noise_1s_rep1: `/home/mohan/depin/experiments/results/20260911T113057Z/f_noise_1s_rep1.txt`
  - noise_1s_rep2: `/home/mohan/depin/experiments/results/20260911T113057Z/f_noise_1s_rep2.txt`
  - noise_1s_rep3: `/home/mohan/depin/experiments/results/20260911T113057Z/f_noise_1s_rep3.txt`
  - noise_1s_rep4: `/home/mohan/depin/experiments/results/20260911T113057Z/f_noise_1s_rep4.txt`
  - noise_output: `/home/mohan/depin/experiments/results/20260911T113057Z/d_noise_output.txt`
  - op_manifest: `/home/mohan/depin/experiments/results/20260911T113057Z/c_op_manifest.json`
  - results: `/home/mohan/depin/experiments/results/20260911T113057Z/b_workload_results.json`
  - samples_0.1s_rep0: `/home/mohan/depin/experiments/results/20260911T113057Z/f_samples_0.1s_rep0.jsonl`
  - samples_0.1s_rep1: `/home/mohan/depin/experiments/results/20260911T113057Z/f_samples_0.1s_rep1.jsonl`
  - samples_0.1s_rep2: `/home/mohan/depin/experiments/results/20260911T113057Z/f_samples_0.1s_rep2.jsonl`
  - samples_0.1s_rep3: `/home/mohan/depin/experiments/results/20260911T113057Z/f_samples_0.1s_rep3.jsonl`
  - samples_0.1s_rep4: `/home/mohan/depin/experiments/results/20260911T113057Z/f_samples_0.1s_rep4.jsonl`
  - samples_1s_rep0: `/home/mohan/depin/experiments/results/20260911T113057Z/f_samples_1s_rep0.jsonl`
  - samples_1s_rep1: `/home/mohan/depin/experiments/results/20260911T113057Z/f_samples_1s_rep1.jsonl`
  - samples_1s_rep2: `/home/mohan/depin/experiments/results/20260911T113057Z/f_samples_1s_rep2.jsonl`
  - samples_1s_rep3: `/home/mohan/depin/experiments/results/20260911T113057Z/f_samples_1s_rep3.jsonl`
  - samples_1s_rep4: `/home/mohan/depin/experiments/results/20260911T113057Z/f_samples_1s_rep4.jsonl`
  - summary: `/home/mohan/depin/experiments/results/20260911T113057Z/f_stop_latency.json`
  - torch_profiler_estimate: `/home/mohan/depin/experiments/results/20260911T113057Z/c_torch_profiler_estimate.json`
  - workload_output: `/home/mohan/depin/experiments/results/20260911T113057Z/d_workload_output.txt`

## 3. 信任假设与证据缺口（spec §7.3-6）

- ncu 存在但 perf counter 权限被拒（ERR_NVGPUCTRPERM 类）：WSL 内核驱动参数由 Windows 侧管理，常规手段无法放开。除非换原生 Linux/放开权限，否则“硬件实测计数”受阻，只能退回估算口径并显式改变计费信任假设。
- 所有计量材料（JSONL/CSV/报告）由 Worker 侧生成：核验组织可以校验签名、哈希与重放一致性，但无法从这些文件本身证明“原始执行确实发生过、且只发生这一次”。GPU 用量可信性与 GPU 身份可信性是两个独立问题（spec §7.3 末段）。
- 本报告只覆盖单机单卡场景；多 GPU/多提供者的按卡归因与跨集群执行未在本阶段验证，对应能力按“未覆盖”处理，不得默认可用。

## 4. 复现

```bash
cd experiments && source .venv/bin/activate
python run_experiment.py                 # 全部 a-g
python run_experiment.py --only a,c      # 子集
```

---

本文件为自动生成的草稿，供整理进 `docs/stage1/gpu-metering-report.md`；结论以各实验的原始产物（同目录 JSON/CSV/TXT）为准。
