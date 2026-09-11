#!/usr/bin/env bash
# down.sh — 停止并清理 depin 开发网络：
#   compose down -v（含数据卷）、删除链码残留容器/镜像、清理生成物。
set -euo pipefail

NETWORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${NETWORK_DIR}/docker-compose.yaml"

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "${COMPOSE_FILE}" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -f "${COMPOSE_FILE}" "$@"
  else
    echo "错误：未找到 docker compose。" >&2
    exit 1
  fi
}

echo "==> 停止并删除容器/网络/数据卷"
compose down --remove-orphans -v

# 清理 peer 构建的链码容器与镜像（fabric 命名前缀 dev-；best-effort，不因缺失而失败）。
CC_CTR_IDS="$(docker ps -aq --filter 'name=dev-' 2>/dev/null || true)"
if [ -n "${CC_CTR_IDS}" ]; then
  echo "==> 删除链码残留容器"
  # shellcheck disable=SC2086
  docker rm -f ${CC_CTR_IDS} >/dev/null 2>&1 || true
fi
CC_IMG_IDS="$(docker images -q 'dev-peer*' 2>/dev/null || true)"
if [ -n "${CC_IMG_IDS}" ]; then
  echo "==> 删除链码构建镜像"
  # shellcheck disable=SC2086
  docker rmi -f ${CC_IMG_IDS} >/dev/null 2>&1 || true
fi
# CCAAS 链码服务镜像（up.sh 每次重新构建，best-effort）
docker rmi -f depin-cc:1.0 >/dev/null 2>&1 || true

echo "==> 清理生成物（organizations / channel-artifacts）"
rm -rf "${NETWORK_DIR}/organizations" "${NETWORK_DIR}/channel-artifacts"

echo "==> 网络已停止并清理完成（bin/ 下二进制保留，bootstrap 可重复利用）。"
