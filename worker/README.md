# depin-worker（stage-1）

基于 Fabric 的 AI 算力共享原型 —— GPU Worker 的第一阶段骨架，对应 GitHub issue #13
「搭建最小系统，验证 GPU 计量」中的 Worker 与 GPU 计量部分。

## stage-1 范围（诚实声明）

本阶段只做两件事：

1. **GPU 环境探测**（`depin_worker/gpu_probe.py`）：一次性探测 GPU 名称 / UUID /
   驱动 / 显存 / compute capability、是否 WSL、NVML 各字段可用性（逐字段
   `ok` / `unavailable` + 原始错误）、每进程占用查询是否可用、nvidia-smi
   路径自动发现与交叉验证。输出 JSON。
2. **NVML 采样**（`depin_worker/nvml_sampler.py`）：后台线程固定间隔采样，
   写 JSONL（整机指标 + 每进程占用），不支持的字段记 `null` 不崩溃。

**不在 stage-1 范围**（后续阶段实现，本包不假装已实现）：心跳与可服务时间累计、
任务领取与执行、计量报告签名、与 Fabric/后端的任何网络交互。
`config.WorkerConfig.heartbeat_interval_seconds` 只是 stage-2 预留占位字段，
当前没有任何代码消费它。

## 设计约束

- **轻量**：运行时依赖只有 `pynvml`；torch 等重依赖放在 `experiments/`，
  `import depin_worker` 不触发 torch 导入（torch 只在 experiments 的实验函数内
  延迟导入）。
- **可测**：pynvml / nvidia-smi 运行器 / 环境变量表全部可注入，测试用 fake
  NVML 模块在无 GPU 机器上跑纯逻辑（见 `tests/fakes.py`）。
- **诚实**：任何字段取不到就记录 `unavailable` + 错误原文，绝不填默认值冒充。
  WSL 上 power/temp/每进程查询可能 N/A，报告里原样体现。

## 安装

```bash
# 系统 Python（WSL 上建议 uv，见 experiments/setup_wsl.sh）
pip install -e worker        # 或 uv pip install -e worker
```

## 使用

```python
from depin_worker.gpu_probe import probe_gpu, probe_gpu_to_json
print(probe_gpu_to_json())

from pathlib import Path
from depin_worker.nvml_sampler import NvmlSampler

with NvmlSampler(Path("samples.jsonl"), interval_seconds=1.0) as sampler:
    ...  # 运行 GPU 负载
print(sampler.stats)
```

命令行手动探测：

```bash
python -m depin_worker.gpu_probe > gpu_probe.json
```

## 输出格式

`probe_gpu()` 报告（节选）：

```json
{
  "schema_version": 1,
  "wsl": {"is_wsl": true, "evidence": ["/proc/version 含 'microsoft'：..."]},
  "nvidia_smi": {"found": true, "path": "/usr/lib/wsl/lib/nvidia-smi"},
  "nvml": {
    "library_available": true,
    "init": {"status": "ok"},
    "driver_version": {"status": "ok", "value": "616.56"},
    "devices": [
      {
        "name": {"status": "ok", "value": "NVIDIA GeForce RTX 4060 Laptop GPU"},
        "uuid": {"status": "ok", "value": "GPU-..."},
        "compute_capability": {"status": "ok", "value": "8.9"},
        "power_draw_w": {"status": "unavailable", "error": "NVMLError_NotSupported: ..."},
        "per_process": {"status": "unavailable", "error": "NVMLError_NotSupported: ..."},
        "unavailable_fields": ["power_draw_w", "power_limit_w", "per_process"]
      }
    ]
  }
}
```

采样 JSONL：首行 `record == "header"`（设备上下文），其后每行一个样本：

```json
{"record": "sample", "ts": 1710000000.12, "seq": 3, "interval_seconds": 1.0,
 "device": {"gpu_util_percent": 95, "memory_used_bytes": 123, "power_draw_w": null},
 "processes": [{"pid": 4242, "used_memory_bytes": 123456, "type": "compute"}],
 "field_errors": {"power_draw_w": "NVMLError_NotSupported: ..."}}
```

## 测试

纯逻辑测试，不需要 GPU / pynvml / torch：

```bash
cd worker
python3 -m unittest discover -s tests -t . -p "test_*.py"
# 或（有 pytest 时，从仓库根目录）
python3 -m pytest worker/tests
```

覆盖：nvidia-smi 路径发现（PATH / WSL 位置 / user 展开）、WSL 多信号判断、
probe 报告 JSON schema 与字段可用性记录、nvidia-smi 交叉验证解析、采样器
启停/上下文管理器/间隔校验/不支持字段置 null/坏行容忍、配置加载校验。

## 已知限制

- 设备身份（GPU UUID）来自驱动自报，stage-1 未做任何防伪造/去重协议设计
  （spec §4.1 的设备身份核验属于后续阶段）。
- 采样器只读 NVML；不尝试 DCGM（WSL 上通常不可用）。
