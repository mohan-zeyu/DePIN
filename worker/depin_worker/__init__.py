"""depin-worker：基于 Fabric 的 AI 算力共享原型 —— GPU Worker。

stage-1 范围（issue #13 的 Worker 与 GPU 计量部分）：
- ``depin_worker.gpu_probe``   一次性 GPU 环境探测，输出 JSON 报告；
- ``depin_worker.nvml_sampler`` 可复用的后台 NVML 采样器（JSONL 输出）。

心跳、任务执行、与链上交互属于后续阶段，本包不实现也不假装已实现。

本包刻意不依赖 torch / CUDA：所有 GPU 访问都通过 pynvml（且支持注入 fake
模块用于纯逻辑测试），pynvml 缺失时仅在真正调用 GPU 的函数内报错。
"""

from .config import ConfigError, SamplerSettings, WorkerConfig

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ConfigError",
    "SamplerSettings",
    "WorkerConfig",
]
