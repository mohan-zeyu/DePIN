"""ncu（Nsight Compute）发现、调用与输出解析。

解析器全部是纯函数，用 experiments/tests/fixtures/ 下的样例文本测试。
实际指标名以 `ncu --query-metrics` 的真实输出为准、原样记录。
"""

from __future__ import annotations

import csv
import glob
import io
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from . import common

#: spec §7.3-2 关注的重点指标（基础名，运行时加 .sum 后缀）。
#: 实际是否存在以 query 结果为准，未命中的原样记录为 missing。
WANTED_METRICS: tuple[str, ...] = (
    "sm__inst_executed_pipe_fp64",
    "sm__inst_executed_pipe_fp32",
    "sm__inst_executed_pipe_fp16",
    "sm__inst_executed_pipe_alu",
    "sm__pipe_tensor_op_hmma_cycles_active",
    "sm__inst_executed_pipe_tensor_op_hmma",
    "sm__inst_executed_pipe_tensor_op_imma",
    "sm__pipe_tensor_cycles_active",
    "sm__cycles_elapsed",
    "sm__cycles_active",
    "gpu__time_duration",
)

_METRIC_NAME_RE = re.compile(r"\b[a-z][a-z0-9_]*(?:__[A-Za-z0-9_.]+)+\b")

_KNOWN_NCU_DIRS = (
    "/usr/local/cuda/bin",
    "/opt/nvidia/nsight-compute",
    "/usr/local/NVIDIA-Nsight-Compute",
)


def find_ncu(
    env: Optional[dict[str, str]] = None,
    extra_candidates: Iterable[str] = (),
) -> tuple[Optional[str], list[str]]:
    """自动发现 ncu 可执行文件。

    查找位置（全部记录到 evidence）：
    1. PATH（含注入 env）；
    2. 当前 Python 解释器同目录（uv/venv 的 .venv/bin —— pip wheel
       nvidia-nsight-compute 会把 ncu 装进环境 bin）；
    3. sys.prefix/bin、~/.local/bin；
    4. /usr/local/cuda*/nsight-compute-*/ncu、/opt/nvidia/nsight-compute*/ncu；
    5. `pip show nvidia-nsight-compute`（仅当 pip 可用）。

    返回 (路径或 None, 发现过程证据列表)。
    """
    evidence: list[str] = []
    checked: list[str] = []

    def probe(path: str) -> Optional[str]:
        if path in checked:
            return None
        checked.append(path)
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        return None

    env_map = dict(os.environ if env is None else env)

    # 1) PATH
    for directory in env_map.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        found = probe(os.path.join(directory, "ncu"))
        if found:
            evidence.append(f"PATH 命中：{found}")
            return found, evidence

    # 2) 解释器同目录（uv venv / pip wheel 安装位置）
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    found = probe(os.path.join(exe_dir, "ncu"))
    if found:
        evidence.append(f"解释器同目录命中（{exe_dir}）：{found}")
        return found, evidence

    # 3) 常见用户/系统位置
    for directory in (
        os.path.join(sys.prefix, "bin"),
        os.path.join(env_map.get("HOME", ""), ".local", "bin"),
        *_KNOWN_NCU_DIRS,
    ):
        found = probe(os.path.join(directory, "ncu"))
        if found:
            evidence.append(f"固定位置命中：{found}")
            return found, evidence

    # 4) 版本化目录 glob
    for pattern in (
        "/usr/local/cuda*/nsight-compute-*/ncu",
        "/opt/nvidia/nsight-compute*/ncu",
        os.path.join(env_map.get("HOME", ""), ".local", "share", "uv", "**", "ncu"),
    ):
        for candidate in sorted(glob.glob(pattern, recursive=True))[:8]:
            found = probe(candidate)
            if found:
                evidence.append(f"glob({pattern}) 命中：{found}")
                return found, evidence

    # 5) pip show（uv 创建的环境默认没有 pip，此路仅作兜底）
    pip_result = common.run_command(
        [sys.executable, "-m", "pip", "show", "-f", "nvidia-nsight-compute"],
        timeout=60,
    )
    if pip_result["returncode"] == 0 and "nvidia-nsight-compute" in (pip_result["stdout"] or ""):
        for line in pip_result["stdout"].splitlines():
            if line.startswith("Location:"):
                base = line.split(":", 1)[1].strip()
                for rel in ("bin/ncu", "../../../bin/ncu"):
                    candidate = os.path.normpath(os.path.join(base, rel))
                    found = probe(candidate)
                    if found:
                        evidence.append(f"pip show 定位命中：{found}")
                        return found, evidence
        evidence.append("pip show 找到包记录但未定位到 ncu 可执行文件")
    else:
        evidence.append(
            "pip show nvidia-nsight-compute 不可用或未安装"
            f"（rc={pip_result['returncode']}）"
        )

    evidence.append(f"未找到 ncu（共检查 {len(checked)} 个候选位置）")
    return None, evidence


def run_ncu_version(ncu_path: str, timeout: float = 60.0) -> dict[str, Any]:
    return common.run_command([ncu_path, "--version"], timeout=timeout)


def run_query_metrics(ncu_path: str, timeout: float = 120.0) -> dict[str, Any]:
    """`ncu --query-metrics` 原始输出（供解析与存档）。"""
    return common.run_command([ncu_path, "--query-metrics"], timeout=timeout)


def extract_metric_names(query_output: str) -> list[str]:
    """从 --query-metrics 文本中提取指标名（纯函数，容错设计）。

    匹配形如 ``prefix__name[.suffix]``（含至少一个双下划线段）的 token；
    描述文字里引用的指标名也会被收入——这无害且符合“原样记录”。
    """
    if not query_output:
        return []
    names = set()
    for match in _METRIC_NAME_RE.finditer(query_output):
        token = match.group(0).rstrip(".")
        names.add(token)
    return sorted(names)


def select_metrics(
    available: Iterable[str],
    wanted: Iterable[str] = WANTED_METRICS,
) -> dict[str, list[str]]:
    """wanted 与 available 的交集/差集（纯函数，原样记录）。"""
    avail = set(available or [])
    found, missing = [], []
    for name in wanted:
        (found if name in avail else missing).append(name)
    return {"found": found, "missing": missing}


def profile_metric_names(base_names: Iterable[str]) -> list[str]:
    """把基础指标名加 .sum 后缀用于 --metrics（纯函数）。"""
    return [n if "." in n else f"{n}.sum" for n in base_names]


def build_profile_command(
    ncu_path: str,
    metrics: Sequence[str],
    target_command: Sequence[str],
    replay_mode: str = "kernel",
    log_file: str | Path = "ncu_profile.csv",
    target_processes: str = "all",
) -> list[str]:
    """构造 ncu 采样命令（纯函数；命令原样记录进结果以便复现）。"""
    return [
        ncu_path,
        "--target-processes",
        target_processes,
        "--replay-mode",
        replay_mode,
        "--metrics",
        ",".join(metrics),
        "--csv",
        "--log-file",
        str(log_file),
        "-f",
        *target_command,
    ]


def parse_profile_csv(text: str) -> dict[str, Any]:
    """解析 `ncu --csv` 的 per-kernel 指标表（纯函数）。

    自动定位表头（含 "Metric Name" 与 "Metric Value" 的行），
    表头前的 ncu 横幅输出被忽略；返回 {"columns", "rows"}。
    """
    if not text:
        return {"columns": [], "rows": [], "banner_lines": []}
    reader = csv.reader(io.StringIO(text))
    rows_raw = [row for row in reader]
    header_idx = None
    banner: list[str] = []
    for idx, row in enumerate(rows_raw):
        cleaned = [c.strip() for c in row]
        if "Metric Name" in cleaned and "Metric Value" in cleaned:
            header_idx = idx
            header = cleaned
            break
        if row:
            banner.append(",".join(row))
    if header_idx is None:
        return {"columns": [], "rows": [], "banner_lines": banner}
    rows: list[dict[str, str]] = []
    for row in rows_raw[header_idx + 1:]:
        if not row or all(not c.strip() for c in row):
            continue
        if len(row) != len(header):
            continue  # 长度不齐的行丢弃（可能有续行/汇总行）
        rows.append({h: v.strip() for h, v in zip(header, row)})
    return {"columns": header, "rows": rows, "banner_lines": banner}


def _to_float(value: str) -> Optional[float]:
    if value is None:
        return None
    cleaned = value.replace(",", "").strip()
    if not cleaned or cleaned.upper() in {"N/A", "NA", "-"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def sum_metric(rows: Sequence[dict[str, str]], metric_name: str) -> dict[str, Any]:
    """对指定指标求和（纯函数）。返回 {total, kernel_count, unparsable}。"""
    total = 0.0
    count = 0
    unparsable = 0
    for row in rows:
        if row.get("Metric Name") != metric_name:
            continue
        value = _to_float(row.get("Metric Value", ""))
        if value is None:
            unparsable += 1
            continue
        total += value
        count += 1
    return {
        "total": total if count else None,
        "kernel_count": count,
        "unparsable": unparsable,
    }


def metric_totals(rows: Sequence[dict[str, str]]) -> dict[str, dict[str, Any]]:
    """全部指标各自求和（纯函数）。"""
    names = sorted({row.get("Metric Name", "") for row in rows if row.get("Metric Name")})
    return {name: sum_metric(rows, name) for name in names}


def extract_pids(rows: Sequence[dict[str, str]]) -> list[str]:
    """CSV 中出现过的 Process ID 集合（纯函数）。"""
    pids = {row.get("Process ID", "") for row in rows if row.get("Process ID")}
    return sorted(p for p in pids if p)


def extract_kernel_names(rows: Sequence[dict[str, str]]) -> list[str]:
    kernels = {row.get("Kernel Name", "") for row in rows if row.get("Kernel Name")}
    return sorted(kernels)


#: kernel 名 → 精度/算子类别的启发式归类（只是启发式，输出中原样保留 kernel 名）
_DOMAIN_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fp64", ("fp64", "double", "dgemm", "dp")),
    ("fp16(hmma/tensor)", ("hgemm", "fp16", "half", "hmma")),
    ("bf16(tensor)", ("bf16",)),
    ("int8(imma/tensor)", ("imma", "int8", "igemm")),
    ("tensor-generic", ("wgmma", "qgmma", "mma", "cutlass")),
)

_FAMILY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gemm", ("gemm", "cutlass", "matmul", "s1688", "s16816")),
    ("conv", ("conv", "cudnn", "implicit", "winograd")),
    ("elementwise", ("elementwise", "vectorized", "copy", "cat_", "index", "fill")),
    ("reduction", ("reduce", "sum", "softmax", "norm", "scan")),
)


def classify_kernel(kernel_name: str) -> dict[str, Any]:
    """kernel 名启发式归类（纯函数；结论只作线索，原始名必须原样保留）。"""
    name = (kernel_name or "").lower()
    domains = [
        domain
        for domain, keywords in _DOMAIN_KEYWORDS
        if any(k in name for k in keywords)
    ] or ["unclassified"]
    families = [
        family
        for family, keywords in _FAMILY_KEYWORDS
        if any(k in name for k in keywords)
    ] or ["other"]
    return {
        "raw": kernel_name,
        "math_domain_hint": domains,
        "op_family_hint": families[0],
        "heuristic": True,
    }


def classify_kernels(kernel_names: Iterable[str]) -> dict[str, Any]:
    """一批 kernel 名的归类汇总（纯函数）。"""
    classified = [classify_kernel(k) for k in kernel_names]
    by_family: dict[str, list[str]] = {}
    by_domain: dict[str, list[str]] = {}
    for item in classified:
        by_family.setdefault(item["op_family_hint"], []).append(item["raw"])
        for domain in item["math_domain_hint"]:
            by_domain.setdefault(domain, []).append(item["raw"])
    return {
        "kernel_count": len(classified),
        "by_op_family": by_family,
        "by_math_domain_hint": by_domain,
        "heuristic": True,
        "note": "按 kernel 名启发式归类，仅作精度/算子覆盖线索；原始 kernel 名见 kernels 列表",
    }


def run_profile(
    ncu_path: str,
    metrics: Sequence[str],
    target_command: Sequence[str],
    log_file: str | Path,
    replay_mode: str = "kernel",
    timeout: float = 900.0,
    target_processes: str = "all",
    cwd: Optional[str | Path] = None,
    env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """执行一次 ncu 采样，返回 {command, run, csv_path, parsed}。"""
    command = build_profile_command(
        ncu_path, metrics, target_command, replay_mode=replay_mode,
        log_file=log_file, target_processes=target_processes,
    )
    run = common.run_command(command, timeout=timeout, cwd=cwd, env=env)
    csv_text = ""
    try:
        csv_text = Path(log_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    parsed = parse_profile_csv(csv_text)
    return {
        "command": command,
        "run": {
            key: run[key]
            for key in ("returncode", "timed_out", "duration_seconds", "stderr")
        },
        "csv_path": str(log_file),
        "csv_size_bytes": len(csv_text.encode("utf-8", errors="replace")),
        "parsed": parsed,
    }


# ---------------------------------------------------------------------------
# 计数权限探测
# ---------------------------------------------------------------------------

_PERMISSION_TORCH_CODE = (
    "import torch;"
    "a=torch.randn(64,64,device='cuda');"
    "b=a@a;"
    "torch.cuda.synchronize();"
    "print('CUDA_OK')"
)


def probe_counter_permission(
    ncu_path: str,
    timeout: float = 240.0,
    python: Optional[str] = None,
) -> dict[str, Any]:
    """实测 ncu 计数（perf counter）权限。

    用一个极小的 CUDA kernel（torch matmul）作为被测进程，检查是否出现
    ERR_NVGPUCTRPERM（Linux 上由内核模块参数 NVreg_RestrictProfilingToAdminUsers
    控制；WSL 的内核驱动由 Windows 侧管理，无法用常规方式修改，只能实测）。
    """
    target = [python or sys.executable, "-c", _PERMISSION_TORCH_CODE]
    log_file = Path(common.DEPIN_EXPERIMENTS_DIR) / ".ncu_permission_probe.csv"
    command = build_profile_command(
        ncu_path,
        ["gpu__time_duration.sum"],
        target,
        replay_mode="kernel",
        log_file=log_file,
        target_processes="all",
    )
    run = common.run_command(command, timeout=timeout)
    combined = f"{run['stdout']}\n{run['stderr']}"
    evidence = common.tail(combined, 1500)
    profiled_rows = 0
    try:
        csv_text = log_file.read_text(encoding="utf-8", errors="replace")
        profiled_rows = len(parse_profile_csv(csv_text)["rows"])
        log_file.unlink(missing_ok=True)
    except OSError:
        pass

    verdict = "unknown"
    if run["timed_out"]:
        verdict = "timeout"
    elif "ERR_NVGPUCTRPERM" in combined:
        verdict = "denied"
    elif run["returncode"] == 0 and profiled_rows > 0:
        verdict = "granted"
    elif run["returncode"] != 0:
        verdict = "error"
    elif run["returncode"] == 0 and profiled_rows == 0:
        # rc=0 但没有任何 kernel 行：torch 可能不可用（stderr 会体现）
        verdict = "error" if "CUDA_OK" not in run["stdout"] else "no-kernels-profiled"
    return {
        "verdict": verdict,
        "returncode": run["returncode"],
        "timed_out": run["timed_out"],
        "profiled_rows": profiled_rows,
        "evidence": evidence,
        "note": "granted=可采集硬件计数；denied=ERR_NVGPUCTRPERM；WSL 内核驱动参数无法常规修改，只能实测",
    }


def find_nvidia_smi_or_none() -> Optional[str]:
    """复用 worker 的 nvidia-smi 自动发现。"""
    if common.ensure_worker_importable():
        from depin_worker.gpu_probe import find_nvidia_smi

        return find_nvidia_smi()
    return None
