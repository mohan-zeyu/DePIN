"""实验 d：attribution_test —— 同机干扰下的任务归因。

负载运行同时启动一个无关 GPU 进程（独立 matmul），检验：
1. `nvidia-smi --query-compute-apps` 能否把两个进程分开；
2. NVML 每进程 API（nvmlDeviceGetComputeRunningProcesses）能否分开；
3. ncu 是否只统计目标进程（被采样的负载进程），不混入干扰进程的 kernel。

对应 spec §7.3 第 3 项（归因到任务）与第 5 项（其他任务是否污染计量）。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from . import common, ncu_runner
from .common import STATUS_BLOCKED, STATUS_PASS, ResultContext


def parse_compute_apps_csv(text: str) -> dict[str, Any]:
    """解析 `nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory
    --format=csv,noheader` 输出（纯函数，fixture 测试）。

    兼容：空输出（无进程）、"[N/A]" 字段、含表头的非 noheader 输出。
    """
    rows: list[dict[str, Any]] = []
    unparsed: list[str] = []
    if not text:
        return {"rows": rows, "unparsed": unparsed}
    from depin_worker.gpu_probe import parse_memory_to_bytes  # 纯函数复用

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("no running processes") or "processes :" in lower or set(line) <= {"=", " ", "-"}:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 1 or not parts[0]:
            unparsed.append(line)
            continue
        pid_raw = parts[0]
        try:
            pid = int(pid_raw)
        except ValueError:
            unparsed.append(line)  # 例如 "[N/A]" 整行
            continue
        rows.append(
            {
                "pid": pid,
                "process_name": parts[1] if len(parts) > 1 else None,
                "used_gpu_memory": parts[2] if len(parts) > 2 else None,
                "used_gpu_memory_bytes": parse_memory_to_bytes(parts[2]) if len(parts) > 2 else None,
                "raw": line,
            }
        )
    return {"rows": rows, "unparsed": unparsed}


def query_smi_compute_apps(smi_path: str, timeout: float = 30.0) -> dict[str, Any]:
    """通过 nvidia-smi 查询当前 GPU 计算进程。"""
    run = common.run_command(
        [
            smi_path,
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader",
        ],
        timeout=timeout,
    )
    parsed = parse_compute_apps_csv(run["stdout"] or "")
    return {
        "returncode": run["returncode"],
        "stderr": common.tail(run["stderr"], 300),
        **parsed,
    }


class _NvmlProcessReader:
    """pynvml 每进程查询的薄封装（不可用则 available=False，不崩溃）。"""

    def __init__(self, device_index: int = 0):
        self.available = False
        self.error: Optional[str] = None
        self._nvml = None
        self._handle = None
        try:
            import pynvml

            self._nvml = pynvml
            init = getattr(pynvml, "nvmlInit_v2", None)
            if init is not None:
                init()
            else:
                pynvml.nvmlInit()
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(device_index)
            # 试一次真实调用
            pynvml.nvmlDeviceGetComputeRunningProcesses(self._handle)
            self.available = True
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    def read(self) -> dict[str, Any]:
        if not self.available:
            return {"available": False, "error": self.error, "processes": None}
        try:
            procs = self._nvml.nvmlDeviceGetComputeRunningProcesses(self._handle)
            return {
                "available": True,
                "processes": [
                    {
                        "pid": int(getattr(p, "pid")),
                        "used_memory_bytes": int(getattr(p, "usedGpuMemory", 0) or 0),
                    }
                    for p in procs
                ],
            }
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": f"{type(exc).__name__}: {exc}", "processes": None}

    def close(self) -> None:
        if self._nvml is not None and self.available:
            try:
                self._nvml.nvmlShutdown()
            except Exception:  # noqa: BLE001
                pass


def run(out: ResultContext, opts: common.ExperimentOptions) -> dict[str, Any]:
    data: dict[str, Any] = {}

    smi_path = ncu_runner.find_nvidia_smi_or_none()
    data["nvidia_smi"] = {"found": smi_path is not None, "path": smi_path}

    nvml_reader = _NvmlProcessReader()
    data["nvml_per_process"] = {
        "available": nvml_reader.available,
        "error": nvml_reader.error,
    }

    # ---- 基线：无（本实验）负载时的进程表 ------------------------------
    baseline_smi = query_smi_compute_apps(smi_path) if smi_path else {"error": "nvidia-smi 不可用", "rows": []}
    data["baseline"] = {
        "smi": {"pids": [r["pid"] for r in baseline_smi.get("rows", [])]},
        "nvml": nvml_reader.read(),
    }

    # ---- 并发阶段：干扰进程 + 主负载 ------------------------------------
    noise_log = out.path("d_noise_output.txt")
    noise = common.spawn_python_module(
        "depin_experiments.matmul_noise",
        ["--seconds", str(opts.attribution_noise_seconds), "--seed", str(opts.seed + 1)],
        stdout_to=noise_log,
    )
    out.register("noise_output", noise_log)
    workload = None
    samples: list[dict[str, Any]] = []
    try:
        time.sleep(2.0)  # 等干扰进程完成 CUDA 初始化
        workload_log = out.path("d_workload_output.txt")
        workload = common.spawn_python_module(
            "depin_experiments.workload_entry",
            [
                "--mode",
                "train",
                "--train-steps",
                str(opts.attribution_train_steps),
                "--precision",
                "fp32",
                "--seed",
                str(opts.seed),
            ],
            stdout_to=workload_log,
        )
        out.register("workload_output", workload_log)
        deadline = time.monotonic() + opts.subprocess_timeout
        while workload.poll() is None and time.monotonic() < deadline:
            sample: dict[str, Any] = {"t": round(time.time(), 3)}
            if smi_path:
                smi = query_smi_compute_apps(smi_path)
                sample["smi_pids"] = sorted(r["pid"] for r in smi.get("rows", []))
                sample["smi_rows_raw"] = [r["raw"] for r in smi.get("rows", [])][:6]
            sample["nvml"] = nvml_reader.read()
            samples.append(sample)
            time.sleep(0.2)
        workload.wait(timeout=60)
    finally:
        if workload is not None and workload.poll() is None:
            workload.kill()
        if noise.poll() is None:
            noise.terminate()
            try:
                noise.wait(timeout=15)
            except Exception:  # noqa: BLE001
                noise.kill()

    workload_pid = workload.pid if workload is not None else None
    noise_pid = noise.pid
    data["pids"] = {"workload": workload_pid, "noise": noise_pid}
    data["samples"] = samples

    smi_separated = bool(
        samples
        and workload_pid
        and any(
            workload_pid in (s.get("smi_pids") or []) and noise_pid in (s.get("smi_pids") or [])
            for s in samples
        )
    )
    nvml_separated = bool(
        samples
        and workload_pid
        and any(
            isinstance(s.get("nvml", {}).get("processes"), list)
            and workload_pid in [p["pid"] for p in s["nvml"]["processes"]]
            and noise_pid in [p["pid"] for p in s["nvml"]["processes"]]
            for s in samples
        )
    )
    data["concurrent_attribution"] = {
        "nvidia_smi_query_compute_apps_separated": smi_separated,
        "nvml_per_process_separated": nvml_separated,
        "nvml_per_process_available": nvml_reader.available,
        "sample_count": len(samples),
    }

    # ---- ncu 只统计目标进程检验 ----------------------------------------
    ncu_attribution: dict[str, Any] = {"tested": False}
    ncu_path, _ev = ncu_runner.find_ncu()
    if ncu_path is None:
        ncu_attribution["blocked_reason"] = "ncu 不可用"
    else:
        permission = ncu_runner.probe_counter_permission(
            ncu_path, timeout=min(opts.subprocess_timeout, 300.0)
        )
        if permission["verdict"] != "granted":
            ncu_attribution["blocked_reason"] = f"计数权限受阻（{permission['verdict']}）"
        else:
            import sys

            noise2_log = out.path("d_noise2_output.txt")
            noise2 = common.spawn_python_module(
                "depin_experiments.matmul_noise",
                ["--seconds", "60", "--seed", str(opts.seed + 2)],
                stdout_to=noise2_log,
            )
            try:
                time.sleep(2.0)
                csv_path = out.path("d_ncu_target_only.csv")
                profile = ncu_runner.run_profile(
                    ncu_path,
                    ["sm__inst_executed_pipe_fp32.sum", "gpu__time_duration.sum"],
                    [
                        sys.executable,
                        "-m",
                        "depin_experiments.workload_entry",
                        "--mode",
                        "infer",
                        "--infer-batches",
                        str(opts.counter_batches),
                        "--precision",
                        "fp32",
                        "--seed",
                        str(opts.seed),
                    ],
                    log_file=csv_path,
                    replay_mode="kernel",
                    timeout=opts.subprocess_timeout,
                    target_processes="all",
                    cwd=common.DEPIN_EXPERIMENTS_DIR,
                )
                rows = profile["parsed"]["rows"]
                pids = ncu_runner.extract_pids(rows)
                kernels = ncu_runner.extract_kernel_names(rows)
                noise2_pid = noise2.pid
                # 被采样的目标进程树（ncu 自身 spawn 的 python）——CSV 里的 pid
                # 应全部属于 workload 子进程，且绝不包含干扰进程 pid
                profiled_target_ok = bool(rows) and str(noise2_pid) not in pids
                ncu_attribution = {
                    "tested": True,
                    "returncode": profile["run"]["returncode"],
                    "csv_pids": pids,
                    "noise_pid": noise2_pid,
                    "noise_pid_absent": str(noise2_pid) not in pids,
                    "kernel_count": len(kernels),
                    "saw_gemm_kernels": any("gemm" in k.lower() for k in kernels),
                    "conclusion": (
                        "ncu 只统计被采样的目标进程树；干扰进程 pid 未出现在 CSV"
                        if profiled_target_ok
                        else "未能确认（无指标行或 pid 异常）"
                    ),
                }
                out.register("ncu_target_only_csv", csv_path)
            finally:
                if noise2.poll() is None:
                    noise2.terminate()
                    try:
                        noise2.wait(timeout=15)
                    except Exception:  # noqa: BLE001
                        noise2.kill()
    data["ncu_target_only"] = ncu_attribution

    # ---- 结论 -----------------------------------------------------------
    ncu_ok = bool(ncu_attribution.get("tested") and ncu_attribution.get("noise_pid_absent"))
    concurrent_ok = smi_separated or nvml_separated
    conclusions = data["conclusions"] = {
        "per_task_attribution": (
            "进程级归因可行：" + ("nvidia-smi 与" if smi_separated else "") + (
                "NVML 每进程 API 可区分负载与干扰进程" if nvml_separated else (
                    "nvidia-smi --query-compute-apps 可区分" if smi_separated else "不可行（见上）"
                )
            )
        ),
        "kernel_level": (
            "ncu 只统计目标进程（实测）" if ncu_ok else "ncu 部分未验证/受阻"
        ),
        "caveats": [
            "pid/进程名/显存占用均由驱动自报，核验方需独立读取才能防伪造报告（spec §7.3-5/6）",
            "WSL 上 NVML 每进程 API 与 nvidia-smi --query-compute-apps 可能受限，本实验如实记录实测结果",
        ],
    }

    nvml_reader.close()
    out.write_json("d_attribution.json", data)
    out.register("summary", out.path("d_attribution.json"))

    if concurrent_ok and ncu_ok:
        status, reason = STATUS_PASS, "并发进程可区分，且 ncu 只统计目标进程"
    elif concurrent_ok:
        status, reason = STATUS_PASS, (
            "进程级归因可行（" + ("smi" if smi_separated else "nvml") + "）；"
            + conclusions["kernel_level"]
        )
    else:
        status = STATUS_BLOCKED
        reason = (
            "nvidia-smi 与 NVML 均无法区分并发进程"
            if not smi_path
            else "并发进程区分失败（详见 samples）"
        )
    return {"status": status, "reason": reason, "data": data, "artifacts": dict(out.artifacts)}
