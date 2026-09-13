#!/usr/bin/env bash
# WSL2 Ubuntu 上搭建 GPU 计量实验环境（stage-1）。
#
# 约束与决策：
# - 系统 Python 3.14 缺 python3-venv/ensurepip，不能标准 venv → 用 uv；
# - 依赖管理走 uv 项目模式（本目录 pyproject.toml + uv.lock + .venv）；
# - 镜像默认用清华 PyPI（可用 UV_DEFAULT_INDEX 覆盖）；
# - nvidia-nsight-compute（ncu 的 pip wheel）装不上时如实失败（退出码 1），
#   不静默跳过——届时 c/e/d 实验的 ncu 部分会记录“受阻”。
#
# 用法（WSL 目标机上，任意目录）：
#   bash experiments/setup_wsl.sh
# 可用环境变量：
#   PYTHON_VERSION   默认 3.12（torch 轮子覆盖最稳；本机系统 3.14 缺 venv）
#   UV_BIN           显式指定 uv 路径（默认 PATH → ~/.local/bin/uv）
#   UV_DEFAULT_INDEX 默认 https://pypi.tuna.tsinghua.edu.cn/simple

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

# ---- 1. 定位 uv（非交互 ssh 下常不在 PATH）--------------------------------
UV_BIN="${UV_BIN:-}"
if [ -z "$UV_BIN" ]; then
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
  elif [ -x "$HOME/.local/bin/uv" ]; then
    UV_BIN="$HOME/.local/bin/uv"
  else
    echo "ERROR: 未找到 uv（PATH 与 ~/.local/bin/uv 均无）。" >&2
    echo "  安装：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
fi
echo "==> uv: $UV_BIN"
echo "==> 镜像: $UV_DEFAULT_INDEX"

# ---- 2. 项目 pyproject.toml（幂等：不存在才创建最小骨架）--------------------
if [ ! -f pyproject.toml ]; then
  cat > pyproject.toml <<'EOF'
[project]
name = "depin-experiments"
version = "0.1.0"
description = "GPU 计量可行性实验（stage-1，spec §7.3）"
requires-python = ">=3.10"
EOF
  echo "==> 已创建 pyproject.toml"
fi

# ---- 3. 创建 .venv（uv 管理的 Python，绕开系统 3.14 缺 ensurepip）----------
echo "==> 创建 .venv（Python ${PYTHON_VERSION}）"
"$UV_BIN" venv --python "$PYTHON_VERSION"

# ---- 4. 主依赖（必须成功）---------------------------------------------------
echo "==> uv add torch / pynvml"
"$UV_BIN" add "torch>=2.4" "pynvml>=11.5"

# ---- 5. ncu（可选：redist archive 用户级安装，免 sudo）-----------------------
# PyPI 无 nvidia-nsight-compute 包（实测 404），ncu 用 NVIDIA redist archive 安装。
# WSL2 / GPU 容器内 ncu 会报 ERR_NVGPUCTRPERM（宿主内核参数，见
# docs/stage1/gpu-metering-report.md §8）；仅原生 Linux（root）可采硬件计数。
NCU_VERSION="2025.1.1.2"
if ! command -v ncu >/dev/null 2>&1; then
  echo "==> 安装 ncu ${NCU_VERSION} 到 ~/opt/nsight-compute（redist archive）"
  if curl -sL --fail -o /tmp/nsight-compute.tar.xz \
      "https://developer.download.nvidia.com/compute/cuda/redist/nsight_compute/linux-x86_64/nsight_compute-linux-x86_64-${NCU_VERSION}-archive.tar.xz"; then
    mkdir -p "$HOME/opt"
    tar -xf /tmp/nsight-compute.tar.xz -C "$HOME/opt"
    ln -sfn "$HOME"/opt/nsight_compute-*-archive "$HOME/opt/nsight-compute"
    echo "  已安装：export PATH=\"\$HOME/opt/nsight-compute:\$PATH\" 后可用"
  else
    echo "警告：ncu 下载失败（网络）；counter_collection / replay_overhead 将记录为受阻" >&2
  fi
else
  echo "==> 检测到 PATH 中已有 ncu，跳过安装"
fi

# ---- 6. 自检 ----------------------------------------------------------------
PY="$SCRIPT_DIR/.venv/bin/python"
echo "==> 环境自检"
"$PY" - <<'EOF'
import sys
print("python:", sys.version.split()[0])
try:
    import torch
    print("torch:", torch.__version__, "cuda_runtime:", torch.version.cuda)
    print("cuda_available:", torch.cuda.is_available())
    if torch.cuda.is_available():
        print("device:", torch.cuda.get_device_name(0))
except Exception as exc:
    print("torch 不可用:", exc)
    sys.exit(1)
try:
    import pynvml
    print("pynvml:", getattr(pynvml, "__version__", "unknown"))
except Exception as exc:
    print("pynvml 不可用:", exc)
    sys.exit(1)
EOF
NCU_BIN=""
if [ -x "$SCRIPT_DIR/.venv/bin/ncu" ]; then
  NCU_BIN="$SCRIPT_DIR/.venv/bin/ncu"
  "$NCU_BIN" --version | head -2 || true
fi

echo ""
echo "==> 完成。运行实验："
echo "    cd $SCRIPT_DIR"
echo "    uv run python run_experiment.py            # 全部 a-g"
echo "    uv run python run_experiment.py --only a   # 先做环境调查"
if [ "$NCU_FAILED" -ne 0 ]; then
  echo ""
  echo "WARNING: ncu 安装失败（上文已说明影响），setup 以失败状态退出。" >&2
  exit 1
fi
