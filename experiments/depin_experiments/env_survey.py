"""实验 a：environment_survey —— GPU 型号/驱动/系统/CUDA 运行时/torch 版本、
ncu 是否可用、计量权限实测。

对应 spec §7.3 第 1 项：GPU 型号、驱动、系统、执行环境及可用计量权限。
"""

from __future__ import annotations

from typing import Any

from . import common, ncu_runner
from .common import STATUS_BLOCKED, STATUS_PASS, ResultContext


def _read_os_release() -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "=" in line:
                    key, _, value = line.strip().partition("=")
                    info[key] = value.strip('"')
    except OSError:
        pass
    return info


def run(out: ResultContext, opts: common.ExperimentOptions) -> dict[str, Any]:
    import platform
    import sys

    data: dict[str, Any] = {"system": {}}
    data["system"] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "os_release": _read_os_release().get("PRETTY_NAME"),
    }

    # WSL 判断（复用 worker 的多信号实现；失败则本地兜底记录错误）
    try:
        if common.ensure_worker_importable():
            from depin_worker.gpu_probe import is_wsl

            wsl, evidence = is_wsl()
        else:
            wsl, evidence = None, ["worker/ 目录不可导入，WSL 判断未执行"]
    except Exception as exc:  # noqa: BLE001
        wsl, evidence = None, [f"is_wsl() 失败: {type(exc).__name__}: {exc}"]
    data["wsl"] = {"is_wsl": wsl, "evidence": evidence}

    # GPU 探测（完整报告存档，摘要进结果）
    try:
        if common.ensure_worker_importable():
            from depin_worker.gpu_probe import probe_gpu, summarize_probe

            gpu_report = probe_gpu()
            out.write_json("a_gpu_probe.json", gpu_report)
            out.register("gpu_probe", out.path("a_gpu_probe.json"))
            data["gpu"] = summarize_probe(gpu_report)
            data["gpu"]["nvml_unavailable_fields_example"] = (
                gpu_report["nvml"]["devices"][0].get("unavailable_fields")
                if gpu_report.get("nvml", {}).get("devices")
                else None
            )
        else:
            data["gpu"] = {"error": "worker/ 不可导入，未执行 NVML 探测"}
    except Exception as exc:  # noqa: BLE001
        data["gpu"] = {"error": f"{type(exc).__name__}: {exc}"}

    # torch / CUDA 运行时
    torch_status: dict[str, Any] = {"available": False}
    try:
        import torch

        torch_status = {
            "available": True,
            "torch_version": torch.__version__,
            "cuda_runtime": torch.version.cuda,
            "cudnn": torch.backends.cudnn.version(),
            "cuda_available": bool(torch.cuda.is_available()),
            "device_count": torch.cuda.device_count(),
        }
        if torch.cuda.is_available():
            torch_status["device_0"] = torch.cuda.get_device_name(0)
            torch_status["bf16_supported"] = bool(torch.cuda.is_bf16_supported())
            cap = torch.cuda.get_device_capability(0)
            torch_status["compute_capability"] = f"{cap[0]}.{cap[1]}"
    except Exception as exc:  # noqa: BLE001
        torch_status["error"] = f"{type(exc).__name__}: {exc}"
    data["torch"] = torch_status

    # ncu 发现 + 版本
    ncu_path, ncu_evidence = ncu_runner.find_ncu()
    ncu_section: dict[str, Any] = {
        "found": ncu_path is not None,
        "path": ncu_path,
        "discovery_evidence": ncu_evidence,
    }
    if ncu_path:
        version_run = ncu_runner.run_ncu_version(ncu_path)
        ncu_section["version"] = {
            "returncode": version_run["returncode"],
            "stdout": common.tail(version_run["stdout"], 800),
            "stderr": common.tail(version_run["stderr"], 400),
        }
    data["ncu"] = ncu_section

    # 计数权限（需要 ncu + torch + CUDA；缺一即受阻并记录原因）
    permission: dict[str, Any]
    if not ncu_path:
        permission = {
            "verdict": "blocked",
            "reason": "ncu 未安装（pip wheel nvidia-nsight-compute 装不上或未装）",
        }
    elif not torch_status.get("cuda_available"):
        permission = {
            "verdict": "blocked",
            "reason": f"torch/CUDA 不可用：{torch_status.get('error') or 'cuda_available=False'}",
        }
    else:
        permission = ncu_runner.probe_counter_permission(
            ncu_path, timeout=min(opts.subprocess_timeout, 300.0)
        )
    data["counter_permission"] = permission
    out.write_json("a_environment.json", data)
    out.register("summary", out.path("a_environment.json"))

    # 状态判定：GPU/环境信息齐全，且 ncu 可用 + 权限 granted 才算通过；
    # ncu 缺失或权限被拒 → 受阻（如实记录，不假装成功）
    gpu_ok = bool(data.get("gpu", {}).get("gpu_present"))
    blockers = []
    if not gpu_ok:
        blockers.append("未探测到可用 GPU")
    if not ncu_path:
        blockers.append("ncu 不可用")
    elif permission.get("verdict") != "granted":
        blockers.append(f"计数权限未获实测通过（verdict={permission.get('verdict')}）")
    if not torch_status.get("cuda_available"):
        blockers.append("torch CUDA 不可用")

    if blockers:
        return {
            "status": STATUS_BLOCKED,
            "reason": "；".join(blockers),
            "data": data,
            "artifacts": dict(out.artifacts),
        }
    return {
        "status": STATUS_PASS,
        "reason": f"环境齐全：{data['gpu'].get('name')}，驱动 {data['gpu'].get('driver_version')}，ncu 权限 granted",
        "data": data,
        "artifacts": dict(out.artifacts),
    }
