"""小型真实 AI 工作负载（spec §7.3 前置：先在真实 GPU 上跑真实负载）。

模型：2 conv + 1 fc 的小 CNN（CIFAR 尺寸 3x32x32，10 类），数据为确定性合成
数据（固定 seed，不下载外部数据集）。训练 N 步 + 批量推理 M 批，
精度覆盖 fp32 / fp16 autocast / bf16（若硬件支持）。

torch 全部延迟导入：本模块导入不依赖 torch，纯逻辑函数（describe_ops、
validate_precision_list）可在无 torch 机器上单测。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from . import common
from .common import STATUS_BLOCKED, STATUS_PASS

WORKLOAD_NAME = "small-cnn-cifar-like"
WORKLOAD_VERSION = "1"
INPUT_SHAPE = (3, 32, 32)
NUM_CLASSES = 10
DATA_POOL_SIZE = 1024


def validate_precision_list(precisions: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """校验精度列表（纯函数）。非法值抛 ValueError。"""
    result = tuple(precisions)
    if not result:
        raise ValueError("精度列表不能为空")
    bad = [p for p in result if p not in common.VALID_PRECISIONS]
    if bad:
        raise ValueError(f"不支持的精度 {bad}；可选 {list(common.VALID_PRECISIONS)}")
    if len(set(result)) != len(result):
        raise ValueError(f"精度列表有重复: {result}")
    return result


def describe_ops(
    batch_size: int = 64,
    channels: int = 3,
    image_size: int = 32,
    conv1_out: int = 16,
    conv2_out: int = 32,
    num_classes: int = 10,
) -> list[dict[str, Any]]:
    """算子/形状清单（纯函数，不依赖 torch）。

    明确说明：这只是 shape 描述，供人核对覆盖面；FLOPs 数字一律不在此
    生成，避免被误当成实测（spec §7.1）。
    """
    if image_size % 4 != 0:
        raise ValueError("image_size 需被两级 2x2 maxpool 整除（%4==0）")
    s1 = image_size // 2
    s2 = image_size // 4
    flat = conv2_out * s2 * s2
    return [
        {
            "op": "conv2d",
            "layer": "conv1",
            "weight_shape": [conv1_out, channels, 3, 3],
            "bias_shape": [conv1_out],
            "input_shape": [batch_size, channels, image_size, image_size],
            "output_shape": [batch_size, conv1_out, image_size, image_size],
        },
        {"op": "relu", "layer": "relu1", "input_shape": [batch_size, conv1_out, image_size, image_size]},
        {"op": "max_pool2d", "layer": "pool1", "kernel": 2, "input_shape": [batch_size, conv1_out, image_size, image_size], "output_shape": [batch_size, conv1_out, s1, s1]},
        {
            "op": "conv2d",
            "layer": "conv2",
            "weight_shape": [conv2_out, conv1_out, 3, 3],
            "bias_shape": [conv2_out],
            "input_shape": [batch_size, conv1_out, s1, s1],
            "output_shape": [batch_size, conv2_out, s1, s1],
        },
        {"op": "relu", "layer": "relu2", "input_shape": [batch_size, conv2_out, s1, s1]},
        {"op": "max_pool2d", "layer": "pool2", "kernel": 2, "input_shape": [batch_size, conv2_out, s1, s1], "output_shape": [batch_size, conv2_out, s2, s2]},
        {"op": "flatten", "layer": "flatten", "input_shape": [batch_size, conv2_out, s2, s2], "output_shape": [batch_size, flat]},
        {
            "op": "linear(gemm+bias)",
            "layer": "fc",
            "weight_shape": [num_classes, flat],
            "bias_shape": [num_classes],
            "input_shape": [batch_size, flat],
            "output_shape": [batch_size, num_classes],
        },
        {"op": "cross_entropy(log_softmax+nll)", "layer": "loss", "input_shape": [batch_size, num_classes], "target_shape": [batch_size]},
    ]


# ---------------------------------------------------------------------------
# torch 延迟导入
# ---------------------------------------------------------------------------


def _torch():
    try:
        import torch  # noqa: PLC0415

        return torch
    except ImportError as exc:
        raise RuntimeError(
            "torch 未安装。请先运行 experiments/setup_wsl.sh（或手动 "
            "`uv add torch pynvml`）。禁止在没有 torch 的情况下假装运行 AI 负载。"
        ) from exc


def _require_cuda(torch) -> None:
    if not torch.cuda.is_available():
        raise RuntimeError(
            "torch.cuda.is_available() == False：本实验必须在真实 NVIDIA GPU 上运行，"
            "不允许用 CPU/空跑冒充（--allow-no-gpu 只用于记录受阻，不会触发本函数）"
        )


def _autocast_args(precision: str) -> dict[str, Any]:
    return {"enabled": precision != "fp32", "dtype": _dtype_for(precision)}


def _dtype_for(precision: str):
    torch = _torch()
    return {"fp32": None, "fp16": torch.float16, "bf16": torch.bfloat16}[precision]


def hardware_precision_support(device: str = "cuda") -> dict[str, Any]:
    """探测硬件对各精度的支持（bf16 依架构而定）。"""
    torch = _torch()
    _require_cuda(torch)
    bf16 = bool(torch.cuda.is_bf16_supported())
    cap = torch.cuda.get_device_capability(0)
    return {
        "fp32": True,
        "fp16": True,
        "bf16": bf16,
        "device": torch.cuda.get_device_name(0),
        "compute_capability": f"{cap[0]}.{cap[1]}",
    }


def build_model(seed: int, device: str):
    torch = _torch()
    nn = torch.nn
    torch.manual_seed(seed)

    class SmallCNN(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1 = nn.Conv2d(3, 16, kernel_size=3, padding=1)
            self.conv2 = nn.Conv2d(16, 32, kernel_size=3, padding=1)
            self.fc = nn.Linear(32 * 8 * 8, 10)

        def forward(self, x):
            x = torch.nn.functional.max_pool2d(torch.nn.functional.relu(self.conv1(x)), 2)
            x = torch.nn.functional.max_pool2d(torch.nn.functional.relu(self.conv2(x)), 2)
            x = torch.flatten(x, 1)
            return self.fc(x)

    model = SmallCNN().to(device)
    return model


def _synthetic_pool(seed: int):
    """确定性合成数据池（CPU 生成，避免设备侧 generator 差异）。"""
    torch = _torch()
    gen = torch.Generator().manual_seed(seed)
    data = torch.randn(DATA_POOL_SIZE, *INPUT_SHAPE, generator=gen)
    targets = torch.randint(0, NUM_CLASSES, (DATA_POOL_SIZE,), generator=gen)
    return data, targets


def _batch_slice(pool, targets, batch_size: int, step: int, device: str):
    start = (step * batch_size) % (DATA_POOL_SIZE - batch_size)
    x = pool[start : start + batch_size].to(device, non_blocking=True)
    y = None
    if targets is not None:  # 推理路径不需要标签
        y = targets[start : start + batch_size].to(device, non_blocking=True)
    return x, y


def run_training(
    *,
    steps: int = 200,
    precision: str = "fp32",
    batch_size: int = 64,
    seed: int = 20260911,
    lr: float = 0.05,
    device: str = "cuda",
    log_every: int = 50,
) -> dict[str, Any]:
    """真实训练：SGD + 交叉熵，N 步，返回损失轨迹与耗时。"""
    validate_precision_list([precision])
    torch = _torch()
    _require_cuda(torch)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    support = hardware_precision_support(device)
    if precision == "bf16" and not support["bf16"]:
        return {"status": "skipped", "reason": "硬件不支持 bf16", "precision": precision}

    model = build_model(seed, device)
    optimizer = torch.optim.SGD(model.parameters(), lr=lr, momentum=0.9)
    pool, targets = _synthetic_pool(seed + 1)

    scaler = None
    if precision == "fp16":
        try:
            scaler = torch.amp.GradScaler("cuda")
        except (AttributeError, TypeError):
            scaler = torch.cuda.amp.GradScaler()

    losses: list[float] = []
    t0 = time.perf_counter()
    for step in range(steps):
        x, y = _batch_slice(pool, targets, batch_size, step, device)
        optimizer.zero_grad(set_to_none=True)
        with torch.autocast("cuda", **_autocast_args(precision)):
            out = model(x)
            loss = torch.nn.functional.cross_entropy(out, y)
        if scaler is not None:
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            optimizer.step()
        losses.append(float(loss.detach().cpu()))
        if log_every and (step + 1) % log_every == 0:
            print(f"train[{precision}] step {step + 1}/{steps} loss={losses[-1]:.4f}", flush=True)
    if device.startswith("cuda"):
        torch.cuda.synchronize()
    elapsed = time.perf_counter() - t0

    tail10 = losses[-10:] if losses else []
    return {
        "status": "ok",
        "precision": precision,
        "steps": steps,
        "batch_size": batch_size,
        "seed": seed,
        "first_loss": losses[0] if losses else None,
        "final_loss": losses[-1] if losses else None,
        "mean_loss_last10": sum(tail10) / len(tail10) if tail10 else None,
        "loss_decreased": bool(losses and losses[-1] < losses[0]),
        "seconds": elapsed,
        "steps_per_second": steps / elapsed if elapsed > 0 else None,
        "loss_every_log": losses[:: max(log_every, 1)][:20] if log_every else losses[:20],
    }


def run_inference(
    *,
    batches: int = 50,
    precision: str = "fp32",
    batch_size: int = 64,
    seed: int = 20260911,
    device: str = "cuda",
    model=None,
) -> dict[str, Any]:
    """真实批量推理：M 批 forward（eval + no_grad），返回时延与输出校验和。"""
    validate_precision_list([precision])
    torch = _torch()
    _require_cuda(torch)
    torch.manual_seed(seed)

    support = hardware_precision_support(device)
    if precision == "bf16" and not support["bf16"]:
        return {"status": "skipped", "reason": "硬件不支持 bf16", "precision": precision}

    if model is None:
        model = build_model(seed, device)
    model.eval()
    pool, _targets = _synthetic_pool(seed + 2)

    checksum = 0.0
    batch_times: list[float] = []
    with torch.no_grad():
        # 预热 3 批（显存/cudnn plan 初始化，不计入统计；是否计费属未定项）
        for warm in range(3):
            x, _ = _batch_slice(pool, None, batch_size, warm, device)
            with torch.autocast("cuda", **_autocast_args(precision)):
                model(x)
        if device.startswith("cuda"):
            torch.cuda.synchronize()
        for batch in range(batches):
            x, _ = _batch_slice(pool, None, batch_size, batch, device)
            t0 = time.perf_counter()
            with torch.autocast("cuda", **_autocast_args(precision)):
                out = model(x)
            if device.startswith("cuda"):
                torch.cuda.synchronize()
            batch_times.append(time.perf_counter() - t0)
            checksum += float(out.sum().detach().cpu())

    return {
        "status": "ok",
        "precision": precision,
        "batches": batches,
        "batch_size": batch_size,
        "seed": seed,
        "warmup_batches": 3,
        "seconds": sum(batch_times),
        "images": batches * batch_size,
        "batch_latency_stats": common.min_median_max(batch_times),
        "output_checksum": checksum,
        "checksum_note": "输出元素累加和，仅用于跨运行一致性核对，不是计量依据",
    }


def run_inference_profiled(
    *,
    batches: int = 8,
    precision: str = "fp32",
    batch_size: int = 64,
    seed: int = 20260911,
    top_k: int = 30,
    device: str = "cuda",
) -> dict[str, Any]:
    """torch.profiler(with_flops=True) 记录一份 FLOPs 估算。

    【必须明确标注】with_flops 的 flops 是基于算子输入 shape 的推导估算，
    不是硬件计数器实测（spec §7.1 禁止把公式估算标为硬件实测）。
    """
    torch = _torch()
    from torch.profiler import ProfilerActivity, profile

    model = build_model(seed, device)
    model.eval()
    pool, _targets = _synthetic_pool(seed + 2)

    with profile(
        activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
        with_flops=True,
    ) as prof:
        with torch.no_grad():
            for batch in range(batches):
                x, _ = _batch_slice(pool, None, batch_size, batch, device)
                with torch.autocast("cuda", **_autocast_args(precision)):
                    model(x)
        if device.startswith("cuda"):
            torch.cuda.synchronize()

    events = []
    for ev in prof.key_averages():
        cuda_time = getattr(ev, "self_device_time_total", None)
        if cuda_time is None:
            cuda_time = getattr(ev, "self_cuda_time_total", 0.0)
        if ev.flops or cuda_time:
            events.append(
                {
                    "name": ev.key,
                    "self_cuda_time_us": cuda_time,
                    "cpu_time_us": ev.self_cpu_time_total,
                    "flops_estimate": ev.flops,
                    "calls": ev.count,
                }
            )
    events.sort(key=lambda e: (-(e["flops_estimate"] or 0), -(e["self_cuda_time_us"] or 0)))
    total_flops = sum(e["flops_estimate"] or 0 for e in events)
    return {
        "estimation_method": (
            "torch.profiler with_flops=True：基于算子输入 shape 的公式推导估算，"
            "不是硬件计数器实测（spec §7.1：性能分析工具的公式估算不能标为硬件实测）"
        ),
        "precision": precision,
        "batches": batches,
        "batch_size": batch_size,
        "top_events_by_flops": events[:top_k],
        "total_flops_estimate": total_flops,
    }


# ---------------------------------------------------------------------------
# 实验 b：训练 + 推理编排
# ---------------------------------------------------------------------------


def run(out: common.ResultContext, opts: common.ExperimentOptions) -> dict[str, Any]:
    """实验 b：workload_train + workload_infer。"""
    try:
        torch = _torch()
        torch_info = {
            "torch_version": torch.__version__,
            "cuda_runtime": torch.version.cuda,
            "cudnn": torch.backends.cudnn.version(),
            "device": torch.cuda.get_device_name(0),
        }
    except RuntimeError as exc:
        return {
            "status": STATUS_BLOCKED,
            "reason": f"torch/CUDA 不可用：{exc}",
            "data": {},
            "artifacts": {},
        }

    precisions = validate_precision_list(opts.precisions)
    support = hardware_precision_support(opts.device)
    manifest = {
        "workload": WORKLOAD_NAME,
        "workload_version": WORKLOAD_VERSION,
        "input_shape": list(INPUT_SHAPE),
        "num_classes": NUM_CLASSES,
        "data": "确定性合成数据（torch.randn/randint，固定 seed，无外部数据集）",
        "ops": describe_ops(batch_size=opts.batch_size),
        "precision_support": support,
    }
    out.write_json("b_workload_manifest.json", manifest)
    out.register("manifest", out.path("b_workload_manifest.json"))

    train_results = {}
    infer_results = {}
    for precision in precisions:
        if precision == "bf16" and not support["bf16"]:
            train_results[precision] = {"status": "skipped", "reason": "硬件不支持 bf16"}
            infer_results[precision] = {"status": "skipped", "reason": "硬件不支持 bf16"}
            continue
        train_results[precision] = run_training(
            steps=opts.train_steps,
            precision=precision,
            batch_size=opts.batch_size,
            seed=opts.seed,
            device=opts.device,
        )
        infer_results[precision] = run_inference(
            batches=opts.infer_batches,
            precision=precision,
            batch_size=opts.batch_size,
            seed=opts.seed,
            device=opts.device,
        )

    data = {
        "torch": torch_info,
        "train": train_results,
        "inference": infer_results,
        "realism_statement": (
            "训练为真实 SGD 反向传播 + 交叉熵；推理为真实 batch forward。"
            "无 sleep/空循环/固定返回值（spec §6.1）。"
        ),
    }
    out.write_json("b_workload_results.json", data)
    out.register("results", out.path("b_workload_results.json"))

    ok_train = [r for r in train_results.values() if r.get("status") == "ok"]
    ok_infer = [r for r in infer_results.values() if r.get("status") == "ok"]
    if not ok_train or not ok_infer:
        return {
            "status": STATUS_BLOCKED,
            "reason": "没有任何精度完成训练+推理",
            "data": data,
            "artifacts": dict(out.artifacts),
        }
    suspicious = [
        p
        for p, r in train_results.items()
        if r.get("status") == "ok" and not r.get("loss_decreased")
    ]
    return {
        "status": STATUS_PASS,
        "reason": (
            f"训练+推理完成（精度 {sorted(p for p, r in train_results.items() if r.get('status') == 'ok')}）"
            + ("；注意：以下精度损失未下降，需人工检查：" + ",".join(suspicious) if suspicious else "")
        ),
        "data": data,
        "artifacts": dict(out.artifacts),
    }
