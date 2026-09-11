"""实验 g：evidence_report —— 汇总 a-f 生成 markdown 报告草稿。

对应 spec §7.3 第 6/7 项：
- 每项能力给出“实测通过／未覆盖／受阻”；
- 核验组织实际取得什么证据、如何检查、哪些部分仍需信任设备所有者。

汇总与渲染逻辑全部是纯函数（build_assessment / build_trust_assumptions /
render_markdown），可用合成结果单测。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from . import common
from .common import (
    STATUS_BLOCKED,
    STATUS_NOT_COVERED,
    STATUS_PASS,
    ResultContext,
)

#: spec §7.3 的七项验证要求 → 支撑实验（字母）
SPEC_73_ITEMS: tuple[dict[str, Any], ...] = (
    {"number": 1, "title": "GPU 型号、驱动、系统、执行环境及可用计量权限", "sources": ("a",)},
    {"number": 2, "title": "使用的硬件计数工具、原始指标、单位、覆盖范围和转换规则", "sources": ("c",)},
    {"number": 3, "title": "是否能区分所需精度与算子类别，如何归因到任务和参与 GPU", "sources": ("c", "d")},
    {"number": 4, "title": "测量开销，是否需要重放，以及如何区分业务执行与测量引起的额外执行", "sources": ("e",)},
    {"number": 5, "title": "同机其他任务、空操作、重复执行或伪造报告是否会污染计量", "sources": ("d", "e")},
    {"number": 6, "title": "核验组织实际取得什么证据、如何检查、哪些部分仍需信任设备所有者", "sources": ("a", "c", "d", "e", "f")},
    {"number": 7, "title": "对每项能力给出“实测通过／未覆盖／受阻”", "sources": ("g",)},
)

_STATUS_RANK = {STATUS_PASS: 2, STATUS_NOT_COVERED: 1, STATUS_BLOCKED: 0}


def item_status_from_sources(statuses: Iterable[Optional[str]]) -> str:
    """状态聚合（纯函数）：

    - 任一来源“受阻” → 受阻（有实测证据表明该项无法完整达成）；
    - 否则任一来源缺失/“未覆盖” → 未覆盖；
    - 全部“实测通过” → 实测通过。
    """
    values = list(statuses)
    if not values:
        return STATUS_NOT_COVERED
    if any(s == STATUS_BLOCKED for s in values):
        return STATUS_BLOCKED
    if any(s != STATUS_PASS for s in values):
        return STATUS_NOT_COVERED
    return STATUS_PASS


def build_assessment(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """把各实验 envelope 映射到 spec §7.3 七项（纯函数）。

    ``results`` 键为实验字母（a-f），值为 envelope（含 status/reason）。
    缺失的实验按“未覆盖”处理。第 7 项由报告自身满足，恒为实测通过
    （报告确实生成了三态标注），但这只是“汇总完成”，不代表各能力通过。
    """
    assessment: list[dict[str, Any]] = []
    for item in SPEC_73_ITEMS:
        sources = item["sources"]
        if item["number"] == 7:
            status = STATUS_PASS
            source_statuses = {"g": STATUS_PASS}
        else:
            source_statuses = {
                letter: (results.get(letter, {}).get("status") if results.get(letter) else None)
                for letter in sources
            }
            status = item_status_from_sources(source_statuses.values())
        notes = []
        for letter, source_status in source_statuses.items():
            name = common.EXPERIMENT_NAMES.get(letter, letter)
            reason = results.get(letter, {}).get("reason") if results.get(letter) else "未运行"
            label = source_status if source_status else STATUS_NOT_COVERED
            notes.append(f"{letter} {name}: {label}（{reason}）")
        assessment.append(
            {
                "number": item["number"],
                "title": item["title"],
                "status": status,
                "source_statuses": source_statuses,
                "notes": notes,
            }
        )
    return assessment


def build_trust_assumptions(results: dict[str, dict[str, Any]]) -> list[str]:
    """信任假设清单（纯函数）：哪些证据缺口意味着仍需信任设备所有者。"""
    bullets: list[str] = []

    def data_of(letter: str) -> dict[str, Any]:
        envelope = results.get(letter) or {}
        return envelope.get("data") if isinstance(envelope.get("data"), dict) else {}

    a = data_of("a")
    c = data_of("c")
    d = data_of("d")
    e = data_of("e")
    f = data_of("f")

    a_status = (results.get("a") or {}).get("status")
    c_status = (results.get("c") or {}).get("status")
    d_status = (results.get("d") or {}).get("status")

    # 1) 硬件计数器
    ncu = (c.get("ncu") or {}) if c else {}
    permission_denied = (
        a_status == STATUS_BLOCKED
        and "verdict" in str(a.get("counter_permission", {}))
        and "denied" in str(a.get("counter_permission", {}))
    ) or ncu.get("counter_permission", {}).get("verdict") == "denied"
    if c_status == STATUS_BLOCKED:
        if ncu.get("found") is False:
            bullets.append(
                "ncu 不可用：本机无法采集硬件计数器，torch.profiler 的 shape 推导 FLOPs "
                "只能作为估算参考（已明确标注，非实测），不足以支撑“按实测运算量计费”。"
                "最小替代方案与改变的信任假设需业务确认（spec §7.3 末段）。"
            )
        elif permission_denied or ncu.get("counter_permission", {}).get("verdict") == "denied":
            bullets.append(
                "ncu 存在但 perf counter 权限被拒（ERR_NVGPUCTRPERM 类）：WSL 内核驱动参数"
                "由 Windows 侧管理，常规手段无法放开。除非换原生 Linux/放开权限，"
                "否则“硬件实测计数”受阻，只能退回估算口径并显式改变计费信任假设。"
            )
        else:
            bullets.append("硬件计数采集受阻（见实验 c 的 blocked_reason）：精度级实测计费暂不可用。")

    # 2) 每进程归因
    if d:
        attribution = d.get("concurrent_attribution", {})
        if attribution.get("nvml_per_process_available") is False:
            bullets.append(
                "NVML 每进程 API 不可用（WSL 常见）：整机利用率无法拆分到进程，"
                "多任务同卡时的计量归因只能依赖 nvidia-smi --query-compute-apps / "
                "ncu 目标进程隔离，或要求单任务独占 GPU。"
            )
        if attribution.get("nvidia_smi_query_compute_apps_separated") is False and attribution.get(
            "nvml_per_process_separated"
        ) is False:
            bullets.append("并发进程在运行时指标上不可区分：存在其他任务污染计量的风险，需独占或进程级证据补强。")

    # 3) 重放开销
    if e:
        overhead = e.get("overhead", {})
        kernel_overhead = overhead.get("kernel_replay_vs_baseline")
        if isinstance(kernel_overhead, (int, float)) and kernel_overhead > 1:
            bullets.append(
                f"ncu kernel replay 实测开销约 {kernel_overhead:.1f} 倍墙钟："
                "重放会引入测量性额外执行，计费口径必须区分业务执行与测量执行"
                "（spec §7.3-4），不允许把重放次数当业务运算量收费。"
            )
        elif e.get("modes", {}).get("application", {}).get("failed"):
            bullets.append("application replay 在本机失败：无法用整应用重放核对计数，只能用 kernel replay 口径。")

    # 4) 停止延迟
    if f:
        intervals = f.get("intervals", {})
        worst_median = None
        for entry in intervals.values():
            latency = (entry.get("idle_observation_latency") or {}).get("median")
            if isinstance(latency, (int, float)):
                worst_median = latency if worst_median is None else max(worst_median, latency)
        if isinstance(worst_median, (int, float)):
            bullets.append(
                f"停止观测延迟实测中位数最高约 {worst_median:.2f}s（含采样间隔量化与 NVML 利用率窗口惯性）："
                "预算保护必须预留安全余量，且不能向发布者收取超托管预算部分（spec §8.2）。"
            )

    # 5) 恒真的信任边界
    bullets.append(
        "所有计量材料（JSONL/CSV/报告）由 Worker 侧生成：核验组织可以校验签名、哈希与"
        "重放一致性，但无法从这些文件本身证明“原始执行确实发生过、且只发生这一次”。"
        "GPU 用量可信性与 GPU 身份可信性是两个独立问题（spec §7.3 末段）。"
    )
    bullets.append(
        "本报告只覆盖单机单卡场景；多 GPU/多提供者的按卡归因与跨集群执行未在本阶段验证，"
        "对应能力按“未覆盖”处理，不得默认可用。"
    )
    return bullets


def render_markdown(
    results: dict[str, dict[str, Any]],
    assessment: list[dict[str, Any]],
    trust: list[str],
    meta: Optional[dict[str, Any]] = None,
) -> str:
    """渲染 markdown 报告草稿（纯函数）。"""
    meta = meta or {}
    lines: list[str] = []
    lines.append("# GPU 计量可行性实验报告（stage-1 草稿）")
    lines.append("")
    lines.append(f"- 生成时间：{meta.get('generated_at', '')}")
    lines.append(f"- 结果目录：{meta.get('results_dir', '')}")
    lines.append(f"- 主机：{meta.get('platform', '')}")
    gpu = meta.get("gpu_summary") or {}
    if gpu:
        lines.append(
            f"- GPU：{gpu.get('name')} / 驱动 {gpu.get('driver_version')} / "
            f"compute capability {gpu.get('compute_capability')} / WSL={gpu.get('is_wsl')}"
        )
    lines.append("")
    lines.append("> 状态口径：实测通过＝有真实运行证据支撑；未覆盖＝本阶段没测；")
    lines.append("> 受阻＝实测后发现环境/权限不满足。禁止把估算标成实测（spec §7.1）。")
    lines.append("")

    lines.append("## 1. spec §7.3 七项要求逐项结论")
    lines.append("")
    lines.append("| # | 要求 | 结论 | 依据实验 |")
    lines.append("| --- | --- | --- | --- |")
    for item in assessment:
        sources = "、".join(f"{k}({common.EXPERIMENT_NAMES.get(k, k)})" for k in item["source_statuses"])
        lines.append(f"| {item['number']} | {item['title']} | {item['status']} | {sources} |")
    lines.append("")

    lines.append("## 2. 各实验明细")
    lines.append("")
    for letter in "abcdef":
        envelope = results.get(letter)
        name = common.EXPERIMENT_NAMES.get(letter, letter)
        if not envelope:
            lines.append(f"### {letter} {name}：{STATUS_NOT_COVERED}")
            lines.append("")
            lines.append("- 本次运行未执行该实验。")
            lines.append("")
            continue
        lines.append(f"### {letter} {name}：{envelope.get('status', STATUS_NOT_COVERED)}")
        lines.append("")
        if envelope.get("reason"):
            lines.append(f"- 结论：{envelope['reason']}")
        if envelope.get("duration_seconds") is not None:
            lines.append(f"- 耗时：{envelope['duration_seconds']:.1f}s")
        artifacts = envelope.get("artifacts") or {}
        if artifacts:
            lines.append("- 产物：")
            for key, path in sorted(artifacts.items()):
                lines.append(f"  - {key}: `{path}`")
        if envelope.get("error"):
            lines.append(f"- 异常：\n\n```\n{common.tail(str(envelope['error']), 1200)}\n```")
        lines.append("")

    lines.append("## 3. 信任假设与证据缺口（spec §7.3-6）")
    lines.append("")
    for bullet in trust:
        lines.append(f"- {bullet}")
    lines.append("")

    lines.append("## 4. 复现")
    lines.append("")
    lines.append("```bash")
    lines.append("cd experiments && source .venv/bin/activate")
    lines.append("python run_experiment.py                 # 全部 a-g")
    lines.append("python run_experiment.py --only a,c      # 子集")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "本文件为自动生成的草稿，供整理进 `docs/stage1/gpu-metering-report.md`；"
        "结论以各实验的原始产物（同目录 JSON/CSV/TXT）为准。"
    )
    return "\n".join(lines) + "\n"


def collect_envelopes(result_dir) -> dict[str, dict[str, Any]]:
    """读取结果目录里 a-f 的 envelope（g 自己排除）。"""
    import json

    envelopes: dict[str, dict[str, Any]] = {}
    for letter, name in common.EXPERIMENT_NAMES.items():
        if letter == "g":
            continue
        path = result_dir / f"{letter}_{name}.json"
        if path.is_file():
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(envelope, dict):
                envelopes[letter] = envelope
    return envelopes


def run(out: ResultContext, opts: common.ExperimentOptions) -> dict[str, Any]:
    results = collect_envelopes(out.result_dir)
    assessment = build_assessment(results)
    trust = build_trust_assumptions(results)

    import platform

    a_data = (results.get("a") or {}).get("data") or {}
    gpu_summary = a_data.get("gpu") if isinstance(a_data.get("gpu"), dict) else {}
    meta = {
        "generated_at": common.utc_now_iso(),
        "results_dir": str(out.result_dir),
        "platform": platform.platform(),
        "gpu_summary": {
            "name": gpu_summary.get("name"),
            "driver_version": gpu_summary.get("driver_version"),
            "compute_capability": gpu_summary.get("compute_capability"),
            "is_wsl": gpu_summary.get("is_wsl"),
        },
    }
    markdown = render_markdown(results, assessment, trust, meta)
    report_path = out.write_text("REPORT.md", markdown)
    out.register("report", report_path)

    data = {
        "meta": meta,
        "assessment": assessment,
        "trust_assumptions": trust,
        "experiments_present": sorted(results),
    }
    out.write_json("g_evidence_report.json", data)
    out.register("summary", out.path("g_evidence_report.json"))

    if results:
        return {
            "status": STATUS_PASS,
            "reason": f"已汇总 {len(results)} 个实验并生成 REPORT.md（各能力结论以表内三态为准）",
            "data": data,
            "artifacts": dict(out.artifacts),
        }
    return {
        "status": STATUS_NOT_COVERED,
        "reason": "结果目录中没有其他实验的产物，报告只包含未覆盖标注",
        "data": data,
        "artifacts": dict(out.artifacts),
    }
