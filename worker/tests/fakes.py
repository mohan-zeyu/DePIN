"""fake NVML 模块：让 worker 测试不依赖真 GPU / pynvml。

模拟两类真实场景：
1. 完整可用的 NVML（普通 Linux 主机）；
2. WSL 风格：temperature 可用、power 等字段 NotSupported、
   每进程 compute 查询可能 NotSupported。
"""

from __future__ import annotations

from typing import Any, Optional


class FakeNvmlError(Exception):
    pass


class NVMLError_NotSupported(FakeNvmlError):
    pass


class NVMLError_NoPermission(FakeNvmlError):
    pass


class NVMLError_Uninitialized(FakeNvmlError):
    pass


class NVMLError_InvalidArgument(FakeNvmlError):
    pass


class FakeMemoryInfo:
    def __init__(self, total: int, used: int, free: int):
        self.total = total
        self.used = used
        self.free = free


class FakeUtilizationRates:
    def __init__(self, gpu: int, memory: int):
        self.gpu = gpu
        self.memory = memory


class FakeProcess:
    def __init__(self, pid: int, used_memory: int):
        self.pid = pid
        self.usedGpuMemory = used_memory


class FakeHandle:
    def __init__(self, index: int):
        self.index = index


class FakeDeviceSpec:
    def __init__(
        self,
        name: str = "NVIDIA GeForce RTX 4060 Laptop GPU",
        uuid: str = "GPU-11111111-2222-3333-4444-555555555555",
        memory_total: int = 8589934592,
        memory_used: int = 0,
        compute_cap: tuple[int, int] = (8, 9),
        temperature: Optional[int] = 47,
        power_mw: Optional[int] = 35000,
        power_limit_mw: Optional[int] = 115000,
        util: tuple[int, int] = (0, 0),
        clocks_sm: Optional[int] = 2100,
        # [] 表示"API 可用且当前无进程"；None 表示"API 在此环境不可用"
        processes: Optional[list] = (),
    ):
        self.name = name
        self.uuid = uuid
        self.memory_total = memory_total
        self.memory_used = memory_used
        self.compute_cap = compute_cap
        self.temperature = temperature
        self.power_mw = power_mw
        self.power_limit_mw = power_limit_mw
        self.util = util
        self.clocks_sm = clocks_sm
        self.processes = list(processes) if processes is not None else None


    def _unsupported(self, what: str):
        raise NVMLError_NotSupported(f"{what} 在此环境不支持（模拟 WSL）")


class FakeNvml:
    """可配置的 pynvml 替身。"""

    NVML_TEMPERATURE_GPU = 0
    NVML_CLOCK_SM = 1
    __name__ = "fake_nvml"

    def __init__(
        self,
        driver_version: str = "616.56",
        devices: Optional[list[FakeDeviceSpec]] = None,
        init_error: Optional[Exception] = None,
        device_count_error: Optional[Exception] = None,
        name_returns_bytes: bool = False,
    ):
        self.driver_version = driver_version
        self.devices = devices if devices is not None else [FakeDeviceSpec()]
        self.init_error = init_error
        self.device_count_error = device_count_error
        self.name_returns_bytes = name_returns_bytes
        self.init_called = False
        self.shutdown_called = False

    # -- 系统/初始化 -------------------------------------------------------

    def nvmlInit(self):
        if self.init_error is not None:
            raise self.init_error
        self.init_called = True

    def nvmlInit_v2(self):
        self.nvmlInit()

    def nvmlShutdown(self):
        self.shutdown_called = True

    def nvmlSystemGetDriverVersion(self):
        return self.driver_version

    def nvmlDeviceGetCount(self):
        if self.device_count_error is not None:
            raise self.device_count_error
        return len(self.devices)

    def nvmlDeviceGetHandleByIndex(self, index: int):
        if not 0 <= index < len(self.devices):
            raise NVMLError_InvalidArgument(f"device index {index} out of range")
        return FakeHandle(index)

    # -- 设备字段 ----------------------------------------------------------

    def _spec(self, handle: FakeHandle) -> FakeDeviceSpec:
        return self.devices[handle.index]

    def nvmlDeviceGetName(self, handle):
        name = self._spec(handle).name
        return name.encode() if self.name_returns_bytes else name

    def nvmlDeviceGetUUID(self, handle):
        return self._spec(handle).uuid

    def nvmlDeviceGetMemoryInfo(self, handle):
        spec = self._spec(handle)
        return FakeMemoryInfo(spec.memory_total, spec.memory_used, spec.memory_total - spec.memory_used)

    def nvmlDeviceGetCudaComputeCapability(self, handle):
        return self._spec(handle).compute_cap

    def nvmlDeviceGetTemperature(self, handle, sensor):
        value = self._spec(handle).temperature
        if value is None:
            self._spec(handle)._unsupported("nvmlDeviceGetTemperature")
        return value

    def nvmlDeviceGetPowerUsage(self, handle):
        value = self._spec(handle).power_mw
        if value is None:
            self._spec(handle)._unsupported("nvmlDeviceGetPowerUsage")
        return value

    def nvmlDeviceGetEnforcedPowerLimit(self, handle):
        value = self._spec(handle).power_limit_mw
        if value is None:
            self._spec(handle)._unsupported("nvmlDeviceGetEnforcedPowerLimit")
        return value

    def nvmlDeviceGetUtilizationRates(self, handle):
        gpu, mem = self._spec(handle).util
        return FakeUtilizationRates(gpu, mem)

    def nvmlDeviceGetClockInfo(self, handle, clock_type):
        value = self._spec(handle).clocks_sm
        if value is None:
            self._spec(handle)._unsupported("nvmlDeviceGetClockInfo")
        return value

    def nvmlDeviceGetComputeRunningProcesses(self, handle):
        spec = self._spec(handle)
        if spec.processes is None:
            # None 显式表示该 API 在此环境不可用
            raise NVMLError_NotSupported(
                "nvmlDeviceGetComputeRunningProcesses 不支持（模拟 WSL）"
            )
        return [FakeProcess(p["pid"], p["used_memory_bytes"]) for p in spec.processes]


def make_full_nvml(**overrides: Any) -> FakeNvml:
    """一切字段可用的 fake（普通 Linux 主机形态）。"""
    spec = FakeDeviceSpec(
        processes=[{"pid": 4242, "used_memory_bytes": 512 * 1024 * 1024}],
        **{k: v for k, v in overrides.items() if k in FakeDeviceSpec.__init__.__code__.co_varnames},
    )
    return FakeNvml(devices=[spec])


def make_wsl_like_nvml() -> FakeNvml:
    """WSL 形态：power/clocks NotSupported、每进程 API 不可用。"""
    spec = FakeDeviceSpec(
        power_mw=None,
        power_limit_mw=None,
        clocks_sm=None,
        temperature=51,
        processes=None,
    )
    return FakeNvml(devices=[spec])
