"""GPU 环境探测（stage-1，issue #13）。

输出一份 JSON 报告，诚实记录：
- GPU 名称 / UUID / 驱动 / 显存总量 / compute capability；
- 是否运行在 WSL 下（多信号判断并保留证据）；
- NVML 各字段逐一 try/except 的结果（``ok`` 或 ``unavailable`` + 原始错误），
  不假设任何字段一定可用（WSL 上 power/temp 等字段常为 N/A）；
- 每进程占用查询（nvmlDeviceGetComputeRunningProcesses）是否可用；
- nvidia-smi 可执行文件路径的自动发现（PATH + /usr/lib/wsl/lib 等常见位置），
  以及一次 nvidia-smi 交叉验证查询。

设计约束：
- pynvml 在函数内部延迟导入；模块导入本身不需要 GPU 或 pynvml；
- 所有外部依赖（nvml 模块、nvidia-smi 运行器、环境变量表）都可注入，
  便于在无 GPU 机器上做纯逻辑单元测试。
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

PROBE_SCHEMA_VERSION = 1

#: nvidia-smi 常见安装位置（PATH 之外的兜底；WSL 非交互 shell 的 PATH
#: 通常不包含 /usr/lib/wsl/lib）。
NVIDIA_SMI_KNOWN_LOCATIONS: tuple[str, ...] = (
    "/usr/lib/wsl/lib/nvidia-smi",
    "/usr/bin/nvidia-smi",
    "/usr/local/bin/nvidia-smi",
    "/usr/local/cuda/bin/nvidia-smi",
    "/usr/lib/linux-nvidia/nvidia-smi",
    "/snap/bin/nvidia-smi",
)

_STATUS_OK = "ok"
_STATUS_UNAVAILABLE = "unavailable"

# ---------------------------------------------------------------------------
# nvidia-smi 路径发现
# ---------------------------------------------------------------------------


def _is_executable_file(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_nvidia_smi(
    env: Optional[Mapping[str, str]] = None,
    extra_candidates: Iterable[str] = (),
) -> Optional[str]:
    """自动发现 nvidia-smi。

    查找顺序：注入的 env（默认 os.environ）的 PATH → extra_candidates →
    常见固定位置。找不到返回 None（不抛异常，由调用方决定如何记录）。
    """
    env_map = os.environ if env is None else env
    # 1) PATH 搜索（手工实现，尊重注入的 env，便于测试）
    for directory in env_map.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, "nvidia-smi")
        if _is_executable_file(candidate):
            return candidate
    # 2) 显式候选与已知位置（相对/绝对路径均可）
    for candidate in (*extra_candidates, *NVIDIA_SMI_KNOWN_LOCATIONS):
        expanded = os.path.expanduser(candidate)
        if _is_executable_file(expanded):
            return expanded
    return None


# ---------------------------------------------------------------------------
# WSL 判断
# ---------------------------------------------------------------------------


def evaluate_wsl_signals(
    proc_version_content: Optional[str],
    env_mapping: Mapping[str, str],
    existing_paths: Iterable[str],
) -> tuple[bool, list[str]]:
    """纯函数：根据各信号判断是否 WSL，返回 (is_wsl, 证据列表)。

    信号（任一命中即判定为 WSL，全部证据原样保留供报告引用）：
    - /proc/version 包含 "microsoft"；
    - 环境变量 WSL_DISTRO_NAME 存在；
    - /mnt/c/Windows 目录存在；
    - /usr/lib/wsl/lib 目录存在（WSL GPU 驱动库挂载点）。
    """
    evidence: list[str] = []
    if proc_version_content and "microsoft" in proc_version_content.lower():
        snippet = " ".join(proc_version_content.split())[:160]
        evidence.append(f"/proc/version 含 'microsoft'：{snippet}")
    distro = env_mapping.get("WSL_DISTRO_NAME")
    if distro:
        evidence.append(f"环境变量 WSL_DISTRO_NAME={distro!r}")
    existing = list(existing_paths)
    if "/mnt/c/Windows" in existing:
        evidence.append("目录 /mnt/c/Windows 存在")
    if "/usr/lib/wsl/lib" in existing:
        evidence.append("目录 /usr/lib/wsl/lib 存在（WSL GPU 驱动库挂载点）")
    return (len(evidence) > 0, evidence)


def is_wsl(
    env: Optional[Mapping[str, str]] = None,
    proc_version_path: str = "/proc/version",
    extra_paths: Iterable[str] = (),
) -> tuple[bool, list[str]]:
    """读取真实系统信号判断是否运行在 WSL。"""
    env_map = os.environ if env is None else env
    content: Optional[str] = None
    try:
        with open(proc_version_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        content = None
    candidates = ["/mnt/c/Windows", "/usr/lib/wsl/lib", *extra_paths]
    existing = [p for p in candidates if os.path.isdir(p)]
    return evaluate_wsl_signals(content, env_map, existing)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _probe_field(fn: Callable[[], Any]) -> dict[str, Any]:
    """执行一次字段读取；成功 → {status: ok, value}，失败 → 诚实记录错误。"""
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 —— 必须把任何 NVML 错误记为 N/A 而不是崩溃
        return {
            "status": _STATUS_UNAVAILABLE,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"status": _STATUS_OK, "value": value}


def _to_str(value: Any) -> str:
    """老版本 pynvml 部分接口返回 bytes。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse_query_gpu_csv(text: str) -> list[dict[str, str]]:
    """解析 `nvidia-smi --query-gpu=... --format=csv,noheader` 输出。

    返回每 GPU 一行的字段字典（值保持原始字符串，未做单位换算）。
    空输出 / 非表格输出 → 空列表（由调用方结合退出码判断）。
    """
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue
        parts = [p.strip() for p in line.split(",")]
        # --query-gpu 请求多少字段就输出多少列；这里无法恢复字段名，
        # 用位置索引占位，字段名由调用方按请求顺序解释。
        rows.append({f"col{i}": v for i, v in enumerate(parts)})
    return rows


_MEMORY_UNITS = {
    "b": 1,
    "kb": 1024,
    "kib": 1024,
    "mb": 1024**2,
    "mib": 1024**2,
    "gb": 1024**3,
    "gib": 1024**3,
}


def parse_memory_to_bytes(value: str) -> Optional[int]:
    """把 "8192 MiB" / "8192MiB" / "8192" 之类的字符串换算成字节。

    无单位时按 MiB 处理（nvidia-smi memory.total 的惯用单位），
    无法解析返回 None。纯函数，供测试。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"N/A", "NA", "[N/A]", "[N/A"}:
        return None
    number = ""
    unit = ""
    for ch in text:
        if ch.isdigit() or ch in ".-+":
            number += ch
        elif not ch.isspace():
            unit += ch
    if not number:
        return None
    try:
        num = float(number)
    except ValueError:
        return None
    unit_key = unit.strip().lower()
    multiplier = _MEMORY_UNITS.get(unit_key, 1024**2) if unit_key else 1024**2
    return int(round(num * multiplier))


def _default_smi_runner(path: str, args: list[str]) -> tuple[int, str, str]:
    """真实执行 nvidia-smi；返回 (returncode, stdout, stderr)。"""
    completed = subprocess.run(
        [path, *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


# ---------------------------------------------------------------------------
# NVML 设备探测
# ---------------------------------------------------------------------------


def _load_nvml(nvml_module: Optional[Any]):
    if nvml_module is not None:
        return nvml_module, None
    try:
        import pynvml  # 延迟导入：模块本身可安全导入
    except Exception as exc:  # noqa: BLE001 —— ImportError 或库加载失败
        return None, f"{type(exc).__name__}: {exc}"
    return pynvml, None


def _nvml_init(nvml: Any) -> tuple[Optional[Any], Optional[str]]:
    try:
        init = getattr(nvml, "nvmlInit_v2", None)
        if init is not None:
            init()
        else:
            nvml.nvmlInit()
        return True, None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def probe_device(nvml: Any, handle: Any, index: int) -> dict[str, Any]:
    """探测单张 GPU：每个字段独立 try/except。"""
    temp_flag = getattr(nvml, "NVML_TEMPERATURE_GPU", 0)

    def memory_total() -> int:
        return int(nvml.nvmlDeviceGetMemoryInfo(handle).total)

    def memory_used() -> int:
        return int(nvml.nvmlDeviceGetMemoryInfo(handle).used)

    def compute_cap() -> str:
        major, minor = nvml.nvmlDeviceGetCudaComputeCapability(handle)
        return f"{int(major)}.{int(minor)}"

    def temperature() -> float:
        return float(nvml.nvmlDeviceGetTemperature(handle, temp_flag))

    def power_draw() -> float:
        return float(nvml.nvmlDeviceGetPowerUsage(handle)) / 1000.0

    def power_limit() -> Optional[float]:
        return float(nvml.nvmlDeviceGetEnforcedPowerLimit(handle)) / 1000.0

    def utilization() -> dict[str, Optional[int]]:
        rates = nvml.nvmlDeviceGetUtilizationRates(handle)
        return {
            "gpu_percent": getattr(rates, "gpu", None),
            "memory_percent": getattr(rates, "memory", None),
        }

    def clocks_sm() -> Optional[int]:
        return int(nvml.nvmlDeviceGetClockInfo(handle, getattr(nvml, "NVML_CLOCK_SM", 0)))

    def compute_processes() -> list[dict[str, Any]]:
        procs = nvml.nvmlDeviceGetComputeRunningProcesses(handle)
        result = []
        for proc in procs:
            result.append(
                {
                    "pid": int(getattr(proc, "pid")),
                    "used_memory_bytes": int(getattr(proc, "usedGpuMemory", 0) or 0),
                }
            )
        return result

    device: dict[str, Any] = {
        "index": index,
        "name": _probe_field(lambda: _to_str(nvml.nvmlDeviceGetName(handle))),
        "uuid": _probe_field(lambda: _to_str(nvml.nvmlDeviceGetUUID(handle))),
        "memory_total_bytes": _probe_field(memory_total),
        "memory_used_bytes": _probe_field(memory_used),
        "compute_capability": _probe_field(compute_cap),
        "temperature_c": _probe_field(temperature),
        "power_draw_w": _probe_field(power_draw),
        "power_limit_w": _probe_field(power_limit),
        "utilization": _probe_field(utilization),
        "clocks_sm_mhz": _probe_field(clocks_sm),
        "per_process": _probe_field(compute_processes),
    }
    unavailable = [key for key, val in device.items() if isinstance(val, dict) and val.get("status") == _STATUS_UNAVAILABLE]
    device["unavailable_fields"] = unavailable
    return device


def _run_smi_cross_check(
    smi_path: Optional[str],
    runner: Callable[[str, list[str]], tuple[int, str, str]],
) -> dict[str, Any]:
    """用 nvidia-smi 做一次交叉验证查询（与 NVML 结果互为印证）。"""
    if not smi_path:
        return {"status": "skipped", "reason": "nvidia-smi 未找到"}
    query_fields = "name,uuid,driver_version,memory.total,compute_cap"
    args = [f"--query-gpu={query_fields}", "--format=csv,noheader"]
    try:
        rc, stdout, stderr = runner(smi_path, args)
    except Exception as exc:  # noqa: BLE001 —— 运行器异常也要诚实记录
        return {"status": _STATUS_UNAVAILABLE, "error": f"{type(exc).__name__}: {exc}"}
    record: dict[str, Any] = {
        "command": [smi_path, *args],
        "returncode": rc,
        "stdout": stdout.strip()[:2000],
        "stderr": stderr.strip()[:2000],
    }
    if rc != 0:
        record["status"] = _STATUS_UNAVAILABLE
        return record
    rows = parse_query_gpu_csv(stdout)
    parsed = []
    field_names = query_fields.split(",")
    for row in rows:
        item: dict[str, Any] = {}
        for i, name in enumerate(field_names):
            key = f"col{i}"
            value = row.get(key)
            if name == "memory.total":
                item[name] = value
                item["memory.total_bytes"] = parse_memory_to_bytes(value or "")
            else:
                item[name] = value
        parsed.append(item)
    record["status"] = _STATUS_OK if parsed else _STATUS_UNAVAILABLE
    if not parsed:
        record["error"] = "stdout 无法解析为表格行"
    record["parsed"] = parsed
    return record


def probe_gpu(
    nvml_module: Optional[Any] = None,
    nvidia_smi: Optional[str] = None,
    smi_runner: Optional[Callable[[str, list[str]], tuple[int, str, str]]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """探测本机 GPU 环境，返回 JSON 兼容 dict。

    参数均可注入用于测试：
    - ``nvml_module``：fake NVML 模块（不依赖真 GPU / pynvml）；
    - ``nvidia_smi``：显式 nvidia-smi 路径（None 则自动发现）；
    - ``smi_runner``：注入的 nvidia-smi 执行器 (path, args) -> (rc, out, err)；
    - ``env``：环境变量表（默认 os.environ）。
    """
    env_map = os.environ if env is None else env
    wsl, wsl_evidence = is_wsl(env=env_map)
    smi_path = nvidia_smi or find_nvidia_smi(env=env_map)

    report: dict[str, Any] = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "python_version": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "wsl": {"is_wsl": wsl, "evidence": wsl_evidence},
        "nvidia_smi": {
            "found": smi_path is not None,
            "path": smi_path,
            "known_locations_checked": list(NVIDIA_SMI_KNOWN_LOCATIONS),
        },
    }

    nvml_section: dict[str, Any] = {"library_available": False, "init": None, "device_count": 0, "devices": []}
    report["nvml"] = nvml_section

    nvml, import_error = _load_nvml(nvml_module)
    if nvml is None:
        nvml_section["import_error"] = import_error
        report["nvidia_smi_probe"] = _run_smi_cross_check(smi_path, smi_runner or _default_smi_runner)
        return report

    nvml_section["library_available"] = True
    nvml_section["module"] = getattr(nvml, "__name__", "nvml")
    init_ok, init_error = _nvml_init(nvml)
    if init_ok is None:
        nvml_section["init"] = {"status": _STATUS_UNAVAILABLE, "error": init_error}
        report["nvidia_smi_probe"] = _run_smi_cross_check(smi_path, smi_runner or _default_smi_runner)
        return report
    nvml_section["init"] = {"status": _STATUS_OK}

    try:
        driver_entry = _probe_field(lambda: _to_str(nvml.nvmlSystemGetDriverVersion()))
        nvml_section["driver_version"] = driver_entry

        count_entry = _probe_field(lambda: int(nvml.nvmlDeviceGetCount()))
        if count_entry["status"] != _STATUS_OK:
            nvml_section["device_count"] = count_entry
        else:
            count = count_entry["value"]
            nvml_section["device_count"] = count
            for index in range(count):
                handle_entry = _probe_field(lambda idx=index: nvml.nvmlDeviceGetHandleByIndex(idx))
                if handle_entry["status"] != _STATUS_OK:
                    nvml_section["devices"].append(
                        {"index": index, "handle": handle_entry}
                    )
                else:
                    nvml_section["devices"].append(probe_device(nvml, handle_entry["value"], index))
    finally:
        try:
            nvml.nvmlShutdown()
        except Exception:  # noqa: BLE001 —— 关闭失败不影响报告
            pass

    report["nvidia_smi_probe"] = _run_smi_cross_check(smi_path, smi_runner or _default_smi_runner)
    return report


def probe_gpu_to_json(
    nvml_module: Optional[Any] = None,
    nvidia_smi: Optional[str] = None,
    smi_runner: Optional[Callable[[str, list[str]], tuple[int, str, str]]] = None,
    env: Optional[Mapping[str, str]] = None,
    indent: int = 2,
) -> str:
    """probe_gpu 的 JSON 字符串便捷封装。"""
    return json.dumps(
        probe_gpu(nvml_module=nvml_module, nvidia_smi=nvidia_smi, smi_runner=smi_runner, env=env),
        ensure_ascii=False,
        indent=indent,
    )


def summarize_probe(report: dict[str, Any]) -> dict[str, Any]:
    """从 probe 报告提取一行式摘要（供日志/心跳后续阶段使用）。"""
    nvml = report.get("nvml", {})
    devices = nvml.get("devices", [])
    first = devices[0] if devices else {}

    def val(key: str) -> Any:
        entry = first.get(key)
        if isinstance(entry, dict) and entry.get("status") == _STATUS_OK:
            return entry.get("value")
        return None

    gpu_present = bool(devices) and isinstance(first.get("name"), dict) and first["name"].get("status") == _STATUS_OK
    return {
        "gpu_present": bool(gpu_present),
        "is_wsl": report.get("wsl", {}).get("is_wsl", False),
        "name": val("name"),
        "uuid": val("uuid"),
        "driver_version": nvml.get("driver_version", {}).get("value")
        if isinstance(nvml.get("driver_version"), dict)
        else None,
        "memory_total_bytes": val("memory_total_bytes"),
        "compute_capability": val("compute_capability"),
        "nvidia_smi_found": report.get("nvidia_smi", {}).get("found", False),
    }


if __name__ == "__main__":  # 手动运行：python -m depin_worker.gpu_probe
    print(probe_gpu_to_json())
