#!/usr/bin/env bash
# bootstrap.sh — 幂等下载 Hyperledger Fabric 2.5.x 二进制（按 uname 选择
# darwin-arm64 / darwin-amd64 / linux-arm64 / linux-amd64）并拉取 docker 镜像。
# 用法：network/scripts/bootstrap.sh [版本号，默认 2.5.10]
set -euo pipefail

FABRIC_VERSION="${1:-2.5.10}"
NETWORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${NETWORK_DIR}/bin"

# ---- 依赖检查：缺 curl/tar 明确报错并退出非 0 ----
for dep in curl tar; do
  if ! command -v "${dep}" >/dev/null 2>&1; then
    echo "错误：缺少依赖 ${dep}，请先安装（bootstrap 需要 curl 与 tar）。" >&2
    exit 1
  fi
done

# ---- 平台选择 ----
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "${ARCH}" in
  aarch64|arm64) ARCH="arm64" ;;
  x86_64|amd64)  ARCH="amd64" ;;
  *)
    echo "错误：不支持的架构 ${ARCH}（支持 amd64/arm64）。" >&2
    exit 1
    ;;
esac
case "${OS}" in
  darwin|linux) ;;
  *)
    echo "错误：不支持的操作系统 ${OS}（支持 darwin/linux）。" >&2
    exit 1
    ;;
esac

echo "==> 平台：${OS}-${ARCH}，Fabric 版本：${FABRIC_VERSION}"

# ---- 幂等下载二进制 ----
already_installed() {
  [ -x "${BIN_DIR}/peer" ] || return 1
  "${BIN_DIR}/peer" version 2>/dev/null | grep -q "Version: ${FABRIC_VERSION}"
}

if already_installed; then
  echo "==> 二进制已存在且版本匹配（${BIN_DIR}），跳过下载"
else
  URL="https://github.com/hyperledger/fabric/releases/download/v${FABRIC_VERSION}/hyperledger-fabric-${OS}-${ARCH}-${FABRIC_VERSION}.tar.gz"
  echo "==> 下载 ${URL}"
  TMP_TAR="$(mktemp -t depin-fabric).tar.gz"
  if ! curl -fSL --retry 3 --connect-timeout 30 -o "${TMP_TAR}" "${URL}"; then
    echo "错误：下载 Fabric 二进制失败，请检查网络或版本号 ${FABRIC_VERSION}。" >&2
    rm -f "${TMP_TAR}"
    exit 1
  fi
  # 压缩包内为 bin/ 与 config/，解压到 network/ 下。
  tar -xzf "${TMP_TAR}" -C "${NETWORK_DIR}"
  rm -f "${TMP_TAR}"
  chmod +x "${BIN_DIR}"/*
  echo "==> 二进制安装到 ${BIN_DIR}"
fi

# ---- 拉取 docker 镜像（多架构，Docker 按宿主架构自动选择） ----
if ! command -v docker >/dev/null 2>&1; then
  echo "错误：未找到 docker 命令，无法拉取镜像。请先安装并启动 Docker。" >&2
  exit 1
fi

for image in peer orderer tools ccenv baseos; do
  echo "==> 拉取 hyperledger/fabric-${image}:${FABRIC_VERSION}"
  docker pull "hyperledger/fabric-${image}:${FABRIC_VERSION}"
done

echo "==> bootstrap 完成。运行 network/scripts/up.sh 启动网络。"
