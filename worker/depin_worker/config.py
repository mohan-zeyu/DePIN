"""Worker 配置（stage-1 最小版）。

只包含采样器需要的配置。心跳 / 任务执行相关字段是后续阶段的占位，
在 stage-1 中不参与任何逻辑，避免把未实现的能力伪装成已配置。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


class ConfigError(ValueError):
    """配置非法。"""


@dataclass(frozen=True)
class SamplerSettings:
    """NVML 采样器配置。"""

    interval_seconds: float = 1.0
    device_index: int = 0
    include_processes: bool = True
    output_path: Path = Path("nvml_samples.jsonl")

    def __post_init__(self) -> None:
        if not isinstance(self.interval_seconds, (int, float)) or isinstance(
            self.interval_seconds, bool
        ):
            raise ConfigError(f"interval_seconds 必须是数字，得到 {self.interval_seconds!r}")
        if not math.isfinite(float(self.interval_seconds)) or self.interval_seconds <= 0:
            raise ConfigError(f"interval_seconds 必须为正有限数，得到 {self.interval_seconds!r}")
        if not isinstance(self.device_index, int) or isinstance(self.device_index, bool):
            raise ConfigError(f"device_index 必须是 int，得到 {self.device_index!r}")
        if self.device_index < 0:
            raise ConfigError(f"device_index 不能为负，得到 {self.device_index}")
        if not isinstance(self.output_path, Path):
            raise ConfigError("output_path 必须是 pathlib.Path")


@dataclass(frozen=True)
class WorkerConfig:
    """Worker 全局配置。

    stage-1 仅使用 sampler 一项；heartbeat_interval_seconds 是 stage-2
    心跳的预留字段，当前阶段没有任何代码消费它。
    """

    sampler: SamplerSettings = field(default_factory=SamplerSettings)
    worker_id: Optional[str] = None
    # 后续阶段（心跳）占位：stage-1 不使用。
    heartbeat_interval_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if self.heartbeat_interval_seconds is not None and not (
            math.isfinite(float(self.heartbeat_interval_seconds))
            and self.heartbeat_interval_seconds > 0
        ):
            raise ConfigError("heartbeat_interval_seconds 若提供必须为正有限数")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sampler"]["output_path"] = str(self.sampler.output_path)
        return data


def config_from_dict(data: dict[str, Any]) -> WorkerConfig:
    """从 dict（JSON 反序列化结果）构造 WorkerConfig，做完整校验。"""
    if not isinstance(data, dict):
        raise ConfigError("配置根节点必须是对象")
    unknown = set(data) - {"sampler", "worker_id", "heartbeat_interval_seconds"}
    if unknown:
        raise ConfigError(f"未知配置项: {sorted(unknown)}")
    sampler_raw = data.get("sampler", {})
    if not isinstance(sampler_raw, dict):
        raise ConfigError("sampler 必须是对象")
    unknown_s = set(sampler_raw) - {
        "interval_seconds",
        "device_index",
        "include_processes",
        "output_path",
    }
    if unknown_s:
        raise ConfigError(f"未知 sampler 配置项: {sorted(unknown_s)}")
    sampler_kwargs = dict(sampler_raw)
    if "output_path" in sampler_kwargs:
        sampler_kwargs["output_path"] = Path(sampler_kwargs["output_path"])
    try:
        sampler = SamplerSettings(**sampler_kwargs)
    except TypeError as exc:  # 签名不匹配
        raise ConfigError(f"sampler 配置字段非法: {exc}") from exc
    kwargs = {
        "sampler": sampler,
        "worker_id": data.get("worker_id"),
        "heartbeat_interval_seconds": data.get("heartbeat_interval_seconds"),
    }
    return WorkerConfig(**kwargs)


def load_config(path: str | Path) -> WorkerConfig:
    """从 JSON 文件加载配置。文件不存在或非法时抛 ConfigError / OSError。"""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 {p} 不是合法 JSON: {exc}") from exc
    return config_from_dict(data)
