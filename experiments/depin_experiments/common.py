"""实验公共工具：状态常量、统计、子进程、结果目录、GPU 门禁。"""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

# spec §7.3-7 的三态结论
STATUS_PASS = "实测通过"
STATUS_NOT_COVERED = "未覆盖"
STATUS_BLOCKED = "受阻"
ALL_STATUSES = (STATUS_PASS, STATUS_NOT_COVERED, STATUS_BLOCKED)

#: 实验字母 → 名称（g 汇总报告用；与 run_experiment.REGISTRY 保持一致）
EXPERIMENT_NAMES: dict[str, str] = {
    "a": "environment_survey",
    "b": "workload_train_infer",
    "c": "counter_collection",
    "d": "attribution_test",
    "e": "replay_overhead",
    "f": "stop_latency",
    "g": "evidence_report",
}

DEPIN_EXPERIMENTS_DIR = Path(__file__).resolve().parent.parent  # experiments/
REPO_ROOT = DEPIN_EXPERIMENTS_DIR.parent
WORKER_DIR = REPO_ROOT / "worker"

VALID_PRECISIONS = ("fp32", "fp16", "bf16")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_results_dir(root: str | Path) -> Path:
    """创建 experiments/results/<UTC 时间戳>/ 目录并返回。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = Path(root)
    out = base / stamp
    counter = 1
    while out.exists():  # 同秒内重跑
        out = base / f"{stamp}-{counter}"
        counter += 1
    out.mkdir(parents=True)
    return out


def min_median_max(values: Iterable[float]) -> dict[str, Any]:
    """延迟分布统计（纯函数）。空输入返回 n=0，不抛异常。"""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"n": 0, "min": None, "median": None, "max": None}
    return {
        "n": len(vals),
        "min": min(vals),
        "median": statistics.median(vals),
        "max": max(vals),
    }


# ---------------------------------------------------------------------------
# 结果目录上下文
# ---------------------------------------------------------------------------


class ResultContext:
    """写入本次实验结果目录的薄封装（JSON / 文本 + 路径登记）。"""

    def __init__(self, result_dir: Path):
        self.result_dir = Path(result_dir)
        self.artifacts: dict[str, str] = {}

    def write_json(self, name: str, obj: Any) -> Path:
        import json

        path = self.result_dir / name
        path.write_text(
            json.dumps(obj, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.result_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def path(self, name: str) -> Path:
        return self.result_dir / name

    def read_json(self, name: str) -> Any:
        import json

        return json.loads((self.result_dir / name).read_text(encoding="utf-8"))

    def register(self, name: str, path: Path) -> Path:
        self.artifacts[name] = str(path)
        return path


# ---------------------------------------------------------------------------
# 子进程
# ---------------------------------------------------------------------------


def run_command(
    command: Sequence[str],
    timeout: float = 900.0,
    cwd: Optional[str | Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """执行命令并完整记录（stdout/stderr 全文 + 截断尾部），不抛异常。"""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else None,
            check=False,
        )
        return {
            "command": list(command),
            "returncode": completed.returncode,
            "timed_out": False,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "duration_seconds": time.monotonic() - started,
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "command": list(command),
            "returncode": None,
            "timed_out": True,
            "stdout": out,
            "stderr": err,
            "duration_seconds": time.monotonic() - started,
        }
    except OSError as exc:  # 可执行文件不存在等
        return {
            "command": list(command),
            "returncode": None,
            "timed_out": False,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "duration_seconds": time.monotonic() - started,
        }


def _python_module_env(extra_env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    env = dict(os.environ)
    parts = [str(DEPIN_EXPERIMENTS_DIR), str(WORKER_DIR)]
    existing = env.get("PYTHONPATH", "")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    return env


def run_python_module(
    module: str,
    args: Sequence[str] = (),
    timeout: float = 900.0,
    extra_env: Optional[Mapping[str, str]] = None,
    python: Optional[str] = None,
) -> dict[str, Any]:
    """以 experiments/ 为 cwd 运行 `python -m <module>`，确保包可导入。"""
    executable = python or sys.executable
    return run_command(
        [executable, "-m", module, *map(str, args)],
        timeout=timeout,
        cwd=DEPIN_EXPERIMENTS_DIR,
        env=_python_module_env(extra_env),
    )


def spawn_python_module(
    module: str,
    args: Sequence[str] = (),
    extra_env: Optional[Mapping[str, str]] = None,
    python: Optional[str] = None,
    stdout_to: Optional[Path] = None,
) -> subprocess.Popen:
    """非阻塞启动 `python -m <module>`（干扰进程 / 停止延迟实验用）。"""
    executable = python or sys.executable
    env = _python_module_env(extra_env)
    if stdout_to is not None:
        fh = open(stdout_to, "wb")
    else:
        fh = subprocess.DEVNULL
    return subprocess.Popen(
        [executable, "-m", module, *map(str, args)],
        cwd=str(DEPIN_EXPERIMENTS_DIR),
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
    )


def parse_result_json(stdout: str) -> Optional[dict[str, Any]]:
    """提取子进程输出里最后一行 `RESULT_JSON={...}`（纯函数）。"""
    if not stdout:
        return None
    marker = "RESULT_JSON="
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith(marker):
            import json

            try:
                obj = json.loads(line[len(marker):])
            except json.JSONDecodeError:
                return None
            return obj if isinstance(obj, dict) else None
    return None


def tail(text: str, limit: int = 2000) -> str:
    """截取文本尾部（写进报告用，纯函数）。"""
    if text is None:
        return ""
    return text if len(text) <= limit else "..." + text[-limit:]


# ---------------------------------------------------------------------------
# worker 包导入 & GPU 门禁
# ---------------------------------------------------------------------------


def ensure_worker_importable() -> bool:
    """把仓库 worker/ 加入 sys.path，使 depin_worker 可导入。"""
    if not WORKER_DIR.is_dir():
        return False
    path_str = str(WORKER_DIR)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
    return True


def check_gpu_available() -> tuple[bool, dict[str, Any]]:
    """显式 GPU 门禁：无 GPU 时由调用方退出非 0，不允许静默通过。"""
    details: dict[str, Any] = {"checks": {}}
    gpu_present = False
    if ensure_worker_importable():
        try:
            from depin_worker.gpu_probe import probe_gpu, summarize_probe

            report = probe_gpu()
            summary = summarize_probe(report)
            details["checks"]["nvml_probe"] = "ok"
            details["gpu_probe"] = summary
            gpu_present = bool(summary.get("gpu_present"))
        except Exception as exc:  # noqa: BLE001 —— 门禁自身不能崩
            details["checks"]["nvml_probe"] = f"error: {type(exc).__name__}: {exc}"
    else:
        details["checks"]["worker_dir"] = f"未找到 {WORKER_DIR}"

    try:
        import torch  # noqa: PLC0415 —— 延迟导入

        available = bool(torch.cuda.is_available())
        details["checks"]["torch_cuda_available"] = available
        details["checks"]["torch_version"] = torch.__version__
        if available:
            gpu_present = True
            details["checks"]["torch_device_0"] = torch.cuda.get_device_name(0)
    except Exception as exc:  # noqa: BLE001
        details["checks"]["torch"] = f"不可用: {type(exc).__name__}: {exc}"
    return gpu_present, details


# ---------------------------------------------------------------------------
# 实验选项
# ---------------------------------------------------------------------------


@dataclass
class ExperimentOptions:
    """所有实验共享的可调参数（CLI 注入）。"""

    seed: int = 20260911
    batch_size: int = 64
    precisions: tuple[str, ...] = VALID_PRECISIONS
    train_steps: int = 200
    infer_batches: int = 50
    counter_steps: int = 12
    counter_batches: int = 8
    replay_batches: int = 10
    attribution_train_steps: int = 80
    attribution_noise_seconds: float = 90.0
    stop_intervals: tuple[float, ...] = (0.1, 1.0)
    stop_reps: int = 5
    stop_warmup_seconds: float = 2.0
    stop_timeout_seconds: float = 20.0
    subprocess_timeout: float = 900.0
    device: str = "cuda"

    def validate(self) -> None:
        bad_precisions = [p for p in self.precisions if p not in VALID_PRECISIONS]
        if bad_precisions:
            raise ValueError(f"不支持的精度 {bad_precisions}，可选 {list(VALID_PRECISIONS)}")
        if not self.precisions:
            raise ValueError("precisions 不能为空")
        for name in (
            "train_steps",
            "infer_batches",
            "counter_steps",
            "counter_batches",
            "replay_batches",
            "attribution_train_steps",
            "stop_reps",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数，得到 {value!r}")
        if not self.stop_intervals or any(i <= 0 for i in self.stop_intervals):
            raise ValueError("stop_intervals 必须非空且均为正数")
        if self.stop_warmup_seconds < 0 or self.stop_timeout_seconds <= 0:
            raise ValueError("stop_warmup_seconds>=0 且 stop_timeout_seconds>0")
        if self.attribution_noise_seconds <= 0:
            raise ValueError("attribution_noise_seconds 必须为正")
        if self.subprocess_timeout <= 0:
            raise ValueError("subprocess_timeout 必须为正")
        if not isinstance(self.seed, int):
            raise ValueError("seed 必须是整数")
        if self.batch_size <= 0:
            raise ValueError("batch_size 必须为正")
