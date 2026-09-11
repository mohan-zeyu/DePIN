"""实验 f：stop_latency —— 停止请求到观测到空闲的延迟分布。

采样间隔两档（默认 100ms / 1s），负载运行中在已知时刻请求停止，
测从停止请求到采样器观察到 GPU 利用率归零/进程退出的延迟，
多次重复取 min/median/max（spec §8.2：停止预算保护必须考虑采样和停止延迟；
spec §7.3-4 的观测能力边界）。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from . import common
from .common import STATUS_BLOCKED, STATUS_PASS, ResultContext


def idle_observation_latency(
    samples: list[dict[str, Any]],
    t_stop: float,
    is_idle_fn=None,
) -> Optional[dict[str, Any]]:
    """纯函数：从 JSONL 样本里找停止后第一个"空闲"样本。

    返回 {latency_seconds, observed_ts, method} 或 None。
    - 优先用每进程列表（processes 为 list 且为空 → 空闲）；
    - 其次整机利用率（gpu_util_percent == 0 → 空闲）；
    - 都不可用 → None（调用方退化为仅进程退出时间）。
    """
    if is_idle_fn is None:
        from depin_worker.nvml_sampler import sample_is_idle

        is_idle_fn = sample_is_idle
    for sample in sorted(samples, key=lambda s: s.get("ts", 0)):
        ts = sample.get("ts")
        if ts is None or ts < t_stop:
            continue  # 停止前的旧样本不算（避免把负载启动前的空闲误当观测）
        idle = is_idle_fn(sample)
        if idle is True:
            if isinstance(sample.get("processes"), list):
                method = "compute-processes-empty"
            elif isinstance((sample.get("device") or {}).get("gpu_util_percent"), (int, float)):
                method = "gpu-util-zero"
            else:
                method = "unknown-signal"
            return {
                "latency_seconds": ts - t_stop,
                "observed_ts": ts,
                "method": method,
            }
    return None


def run(out: ResultContext, opts: common.ExperimentOptions) -> dict[str, Any]:
    # worker 采样器是本实验的被测对象，必须可导入
    try:
        if not common.ensure_worker_importable():
            raise ImportError(f"未找到 {common.WORKER_DIR}")
        from depin_worker.nvml_sampler import NvmlSampler
    except ImportError as exc:
        return {
            "status": STATUS_BLOCKED,
            "reason": f"depin_worker 不可导入：{exc}",
            "data": {},
            "artifacts": {},
        }

    data: dict[str, Any] = {"intervals": {}}
    any_observed = False
    fatal: Optional[str] = None

    for interval in opts.stop_intervals:
        reps: list[dict[str, Any]] = []
        for rep in range(opts.stop_reps):
            jsonl = out.path(f"f_samples_{interval:g}s_rep{rep}.jsonl")
            sampler = NvmlSampler(
                jsonl,
                interval_seconds=interval,
                include_processes=True,
                name=f"stop-latency-{interval:g}s",
            )
            noise_log = out.path(f"f_noise_{interval:g}s_rep{rep}.txt")
            try:
                sampler.start()
            except Exception as exc:  # noqa: BLE001 —— 无 GPU/驱动：如实受阻
                fatal = f"{type(exc).__name__}: {exc}"
                break

            proc = common.spawn_python_module(
                "depin_experiments.matmul_noise",
                ["--seconds", "300", "--seed", str(opts.seed + rep)],
                stdout_to=noise_log,
            )
            out.register(f"noise_{interval:g}s_rep{rep}", noise_log)
            try:
                warmup = max(opts.stop_warmup_seconds, 3 * interval)
                time.sleep(warmup)
                t_stop = time.time()
                proc.terminate()
                # 进程退出时间（紧密轮询，5ms 粒度）
                t_exit: Optional[float] = None
                exit_deadline = time.time() + opts.stop_timeout_seconds
                while time.time() < exit_deadline:
                    if proc.poll() is not None:
                        t_exit = time.time()
                        break
                    time.sleep(0.005)
                if t_exit is None:
                    try:
                        proc.wait(timeout=5)
                        t_exit = time.time()
                    except Exception:  # noqa: BLE001
                        proc.kill()
                        t_exit = time.time()
                # 等采样器观察到空闲（或超时）
                observed: Optional[dict[str, Any]] = None
                deadline = time.time() + opts.stop_timeout_seconds
                while time.time() < deadline:
                    latest = sampler.latest_sample
                    if latest is not None and latest.get("ts", 0) >= t_stop:
                        from depin_worker.nvml_sampler import sample_is_idle

                        if sample_is_idle(latest) is True:
                            observed = {
                                "latency_seconds": latest["ts"] - t_stop,
                                "method": (
                                    "compute-processes-empty"
                                    if isinstance(latest.get("processes"), list)
                                    else "gpu-util-zero"
                                ),
                            }
                            break
                    time.sleep(min(0.02, interval / 4))
                # 多等一个周期，确保后续样本也落盘
                time.sleep(interval + 0.05)
            finally:
                if proc.poll() is None:
                    proc.kill()
                stats = sampler.stop()

            from depin_worker.nvml_sampler import load_samples

            samples = load_samples(jsonl)
            out.register(f"samples_{interval:g}s_rep{rep}", jsonl)
            recorded_observed = observed or idle_observation_latency(samples, t_stop)
            reps.append(
                {
                    "rep": rep,
                    "interval_seconds": interval,
                    "t_stop_epoch": t_stop,
                    "process_exit_latency_seconds": (t_exit - t_stop) if t_exit else None,
                    "idle_observation": recorded_observed,
                    "sampler_samples": stats["samples_written"],
                    "sampler_field_errors": stats["field_error_counts"],
                }
            )
            if recorded_observed:
                any_observed = True
            if sampler.fatal_error:
                fatal = sampler.fatal_error
        if fatal:
            break
        exit_latencies = [r["process_exit_latency_seconds"] for r in reps if r["process_exit_latency_seconds"] is not None]
        obs_latencies = [
            r["idle_observation"]["latency_seconds"]
            for r in reps
            if r.get("idle_observation")
        ]
        data["intervals"][f"{interval:g}s"] = {
            "interval_seconds": interval,
            "reps": reps,
            "process_exit_latency": common.min_median_max(exit_latencies),
            "idle_observation_latency": common.min_median_max(obs_latencies),
            "observation_methods": sorted(
                {r["idle_observation"]["method"] for r in reps if r.get("idle_observation")}
            ),
            "note": "观测延迟受采样间隔量化（≤ interval + 处理时延）；NVML 利用率窗口本身有约 1s 惯性，实测值可能大于一个采样周期",
        }

    data["fatal_error"] = fatal
    out.write_json("f_stop_latency.json", data)
    out.register("summary", out.path("f_stop_latency.json"))

    if fatal:
        return {
            "status": STATUS_BLOCKED,
            "reason": f"采样器无法启动/致命错误：{fatal}",
            "data": data,
            "artifacts": dict(out.artifacts),
        }
    if not data.get("intervals"):
        return {
            "status": STATUS_BLOCKED,
            "reason": "没有任何间隔档完成（见 fatal_error / reps）",
            "data": data,
            "artifacts": dict(out.artifacts),
        }
    reason = "每档间隔均完成；" + (
        "空闲观测信号可用" if any_observed else "仅进程退出信号可用（每进程/利用率字段不可用）"
    )
    return {
        "status": STATUS_PASS,
        "reason": reason,
        "data": data,
        "artifacts": dict(out.artifacts),
    }
