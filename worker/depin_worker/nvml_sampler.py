"""可复用的 NVML 后台采样器（stage-1）。

设计要点：
- 后台守护线程，固定间隔采样，逐行写 JSONL（每行一个 JSON 对象）；
- 第一行是 ``record == "header"`` 的头部（设备名/UUID/启动时间等上下文）；
- 每个字段独立 try/except：取不到的字段值为 ``null``，并在同行的
  ``field_errors`` 里记录错误（诚实记录 WSL 上不可用的字段，绝不崩溃）；
- 支持 start/stop 与上下文管理器；stop 幂等；
- pynvml 延迟导入，模块导入不依赖 GPU；测试可注入 fake NVML 模块。

JSONL 样例行（sample）::

    {"record": "sample", "ts": 1710000000.123, "seq": 3,
     "interval_seconds": 1.0,
     "device": {"gpu_util_percent": 95, "mem_util_percent": 40,
                "memory_used_bytes": 123, "memory_total_bytes": 8589934592,
                "temperature_c": null, "power_draw_w": null,
                "power_limit_w": null, "clocks_sm_mhz": null},
     "processes": [{"pid": 4242, "used_memory_bytes": 123456,
                    "type": "compute"}],
     "field_errors": {"temperature_c": "NVMLError_NotSupported: ..."}}
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Union

RECORD_HEADER = "header"
RECORD_SAMPLE = "sample"

FieldErrors = dict[str, str]


class NvmlSamplerError(RuntimeError):
    """采样器初始化失败（pynvml 缺失 / NVML 初始化失败等）。"""


def _load_nvml(nvml_module: Optional[Any]) -> Any:
    if nvml_module is not None:
        return nvml_module
    try:
        import pynvml  # noqa: PLC0415 —— 刻意延迟导入
    except Exception as exc:  # noqa: BLE001
        raise NvmlSamplerError(
            f"无法导入 pynvml（{type(exc).__name__}: {exc}）；"
            "请安装 pynvml（worker/pyproject.toml 的唯一运行时依赖）"
        ) from exc
    return pynvml


def _to_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class NvmlSampler:
    """后台 NVML 采样器。

    用法::

        with NvmlSampler(Path("samples.jsonl"), interval_seconds=0.1) as sampler:
            run_gpu_workload()
        stats = sampler.stats
    """

    def __init__(
        self,
        output_path: Union[str, Path],
        interval_seconds: float = 1.0,
        device_index: int = 0,
        nvml_module: Optional[Any] = None,
        include_processes: bool = True,
        clock: Callable[[], float] = time.time,
        name: str = "nvml-sampler",
    ) -> None:
        if not isinstance(interval_seconds, (int, float)) or isinstance(interval_seconds, bool):
            raise ValueError(f"interval_seconds 必须是数字：{interval_seconds!r}")
        if not math.isfinite(float(interval_seconds)) or interval_seconds <= 0:
            raise ValueError(f"interval_seconds 必须为正有限数：{interval_seconds!r}")
        if device_index < 0:
            raise ValueError(f"device_index 不能为负：{device_index}")
        self.output_path = Path(output_path)
        self.interval_seconds = float(interval_seconds)
        self.device_index = int(device_index)
        self.include_processes = bool(include_processes)
        self._nvml_module = nvml_module
        self._clock = clock
        self.name = name

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fh: Optional[Any] = None
        self._nvml: Optional[Any] = None
        self._handle: Optional[Any] = None
        self._lock = threading.Lock()

        self._samples_written = 0
        self._field_error_counts: Counter[str] = Counter()
        self._fatal_error: Optional[str] = None
        # 最近若干样本，供外部线程快速查询（如停止延迟实验观察利用率归零）
        self._recent: deque[dict[str, Any]] = deque(maxlen=16)

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> "NvmlSampler":
        """初始化 NVML 并启动采样线程。初始化失败同步抛 NvmlSamplerError。"""
        with self._lock:
            if self._thread is not None:
                raise NvmlSamplerError("采样器已启动，不能重复 start()")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._nvml = _load_nvml(self._nvml_module)
        try:
            init = getattr(self._nvml, "nvmlInit_v2", None)
            if init is not None:
                init()
            else:
                self._nvml.nvmlInit()
        except Exception as exc:  # noqa: BLE001
            raise NvmlSamplerError(
                f"NVML 初始化失败（{type(exc).__name__}: {exc}）；"
                "常见原因：无 NVIDIA 驱动、WSL 下 /usr/lib/wsl/lib 不在库搜索路径"
            ) from exc
        try:
            self._handle = self._nvml.nvmlDeviceGetHandleByIndex(self.device_index)
        except Exception as exc:  # noqa: BLE001
            try:
                self._nvml.nvmlShutdown()
            except Exception:  # noqa: BLE001
                pass
            raise NvmlSamplerError(
                f"获取 GPU#{self.device_index} 句柄失败（{type(exc).__name__}: {exc}）"
            ) from exc

        self._fh = open(self.output_path, "a", encoding="utf-8", buffering=1)
        header = {
            "record": RECORD_HEADER,
            "sampler": self.name,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "interval_seconds": self.interval_seconds,
            "device_index": self.device_index,
            "device": self._describe_device(),
        }
        self._write_line(header)

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> dict[str, Any]:
        """停止采样线程并关闭文件。幂等；返回统计。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None
            if self._nvml is not None:
                try:
                    self._nvml.nvmlShutdown()
                except Exception:  # noqa: BLE001
                    pass
                self._nvml = None
            self._thread = None
        return self.stats

    def __enter__(self) -> "NvmlSampler":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # -- 观测 -------------------------------------------------------------

    @property
    def samples_written(self) -> int:
        return self._samples_written

    @property
    def latest_sample(self) -> Optional[dict[str, Any]]:
        """最近一次样本（跨线程读取；可能略滞后，不保证每次不同）。"""
        if not self._recent:
            return None
        return self._recent[-1]

    @property
    def fatal_error(self) -> Optional[str]:
        return self._fatal_error

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "output_path": str(self.output_path),
            "interval_seconds": self.interval_seconds,
            "samples_written": self._samples_written,
            "field_error_counts": dict(self._field_error_counts),
            "fatal_error": self._fatal_error,
        }

    # -- 内部 -------------------------------------------------------------

    def _describe_device(self) -> dict[str, Any]:
        nvml, handle = self._nvml, self._handle
        device: dict[str, Any] = {}
        try:
            device["name"] = _to_str(nvml.nvmlDeviceGetName(handle))
        except Exception as exc:  # noqa: BLE001
            device["name"] = None
            device["name_error"] = f"{type(exc).__name__}: {exc}"
        try:
            device["uuid"] = _to_str(nvml.nvmlDeviceGetUUID(handle))
        except Exception as exc:  # noqa: BLE001
            device["uuid"] = None
            device["uuid_error"] = f"{type(exc).__name__}: {exc}"
        return device

    def _field(self, key: str, fn: Callable[[], Any], errors: FieldErrors) -> Any:
        """读一个字段；失败 → None 并记录错误。"""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 —— 单字段失败不允许拖垮整次采样
            errors[key] = f"{type(exc).__name__}: {exc}"
            self._field_error_counts[key] += 1
            return None

    def _sample_once(self) -> dict[str, Any]:
        nvml, handle = self._nvml, self._handle
        errors: FieldErrors = {}
        temp_flag = getattr(nvml, "NVML_TEMPERATURE_GPU", 0)
        sm_flag = getattr(nvml, "NVML_CLOCK_SM", 0)

        def utilization() -> dict[str, Optional[int]]:
            rates = nvml.nvmlDeviceGetUtilizationRates(handle)
            return {
                "gpu_util_percent": getattr(rates, "gpu", None),
                "mem_util_percent": getattr(rates, "memory", None),
            }

        util = self._field("utilization", utilization, errors) or {}
        device_metrics: dict[str, Any] = {
            "gpu_util_percent": util.get("gpu_util_percent"),
            "mem_util_percent": util.get("mem_util_percent"),
            "memory_used_bytes": self._field(
                "memory_used_bytes",
                lambda: int(nvml.nvmlDeviceGetMemoryInfo(handle).used),
                errors,
            ),
            "memory_total_bytes": self._field(
                "memory_total_bytes",
                lambda: int(nvml.nvmlDeviceGetMemoryInfo(handle).total),
                errors,
            ),
            "temperature_c": self._field(
                "temperature_c",
                lambda: float(nvml.nvmlDeviceGetTemperature(handle, temp_flag)),
                errors,
            ),
            "power_draw_w": self._field(
                "power_draw_w",
                lambda: float(nvml.nvmlDeviceGetPowerUsage(handle)) / 1000.0,
                errors,
            ),
            "power_limit_w": self._field(
                "power_limit_w",
                lambda: float(nvml.nvmlDeviceGetEnforcedPowerLimit(handle)) / 1000.0,
                errors,
            ),
            "clocks_sm_mhz": self._field(
                "clocks_sm_mhz",
                lambda: int(nvml.nvmlDeviceGetClockInfo(handle, sm_flag)),
                errors,
            ),
        }
        processes: Any = None
        if self.include_processes:
            def list_processes() -> list[dict[str, Any]]:
                procs = nvml.nvmlDeviceGetComputeRunningProcesses(handle)
                return [
                    {
                        "pid": int(getattr(proc, "pid")),
                        "used_memory_bytes": int(getattr(proc, "usedGpuMemory", 0) or 0),
                        "type": "compute",
                    }
                    for proc in procs
                ]

            # 每进程占用不可用（WSL 常见）→ null + 错误记录，而不是崩溃
            processes = self._field("processes", list_processes, errors)

        return {
            "record": RECORD_SAMPLE,
            "ts": self._clock(),
            "seq": self._samples_written,
            "interval_seconds": self.interval_seconds,
            "device": device_metrics,
            "processes": processes,
            "field_errors": errors or None,
        }

    def _write_line(self, obj: dict[str, Any]) -> None:
        assert self._fh is not None
        self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def _run(self) -> None:
        period = self.interval_seconds
        next_tick = self._clock()
        while not self._stop_event.is_set():
            try:
                sample = self._sample_once()
            except Exception as exc:  # noqa: BLE001 —— 线程内兜底，不允许静默死亡
                self._fatal_error = f"{type(exc).__name__}: {exc}"
                try:
                    self._write_line(
                        {
                            "record": "sample",
                            "ts": self._clock(),
                            "seq": self._samples_written,
                            "interval_seconds": self.interval_seconds,
                            "device": None,
                            "processes": None,
                            "field_errors": {"__fatal__": self._fatal_error},
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass
                self._stop_event.set()
                break
            with self._lock:
                if self._fh is None:  # 已 stop
                    break
                self._write_line(sample)
            self._recent.append(sample)
            self._samples_written += 1
            next_tick += period
            delay = next_tick - self._clock()
            if delay > 0:
                # 用 event.wait 而不是 sleep，保证 stop() 能立刻打断
                if self._stop_event.wait(delay):
                    break
            else:
                # 采样慢于周期：重新对齐，避免欠账累积成连续狂采
                next_tick = self._clock()


# ---------------------------------------------------------------------------
# JSONL 读取工具
# ---------------------------------------------------------------------------


def iter_jsonl(path: Union[str, Path]) -> Iterator[dict[str, Any]]:
    """逐行读取 JSONL；坏行跳过（yields 合法 dict）。"""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def load_samples(path: Union[str, Path]) -> list[dict[str, Any]]:
    """只返回 record == "sample" 的行。"""
    return [obj for obj in iter_jsonl(path) if obj.get("record") == RECORD_SAMPLE]


def load_header(path: Union[str, Path]) -> Optional[dict[str, Any]]:
    """返回头部行（若存在）。"""
    for obj in iter_jsonl(path):
        if obj.get("record") == RECORD_HEADER:
            return obj
    return None


def sample_is_idle(sample: Optional[dict[str, Any]]) -> Optional[bool]:
    """判断一个样本是否代表 GPU 空闲。

    返回三值逻辑：
    - True：确认空闲（无 compute 进程，或利用率归零）；
    - False：确认忙碌；
    - None：无法判断（字段缺失/不支持）。
    """
    if not isinstance(sample, dict):
        return None
    processes = sample.get("processes")
    if isinstance(processes, list):
        if processes:
            return False
        # 进程列表为空 → 空闲（该字段可用时的最强证据）
        return True
    # 每进程字段不可用 → 退回整机利用率
    device = sample.get("device") or {}
    util = device.get("gpu_util_percent")
    if isinstance(util, (int, float)):
        return util <= 0
    return None
