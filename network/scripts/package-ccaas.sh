#!/usr/bin/env bash
# 打包 external 型链码包（官方 cc_service 格式）：
#   metadata.json  {"type":"external","label":"..."}
#   code.tar.gz    connection.json（顶层）+ metadata/
# peer 侧由 external-builder 处理并按 connection.address 主动连接链码服务。
# 用法：package-ccaas.sh <输出.tar.gz> <链码地址 host:port>
set -euo pipefail

OUT="${1:?用法: package-ccaas.sh 输出.tar.gz 链码地址}"
ADDR="${2:?用法: package-ccaas.sh 输出.tar.gz 链码地址}"
LABEL="depincc_1.0"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

cat > "${WORK}/metadata.json" <<EOF
{
  "type": "ccaas",
  "label": "${LABEL}"
}
EOF

cat > "${WORK}/connection.json" <<EOF
{
  "address": "${ADDR}",
  "dial_timeout": "10s",
  "tls_required": false
}
EOF

mkdir -p "${WORK}/pkg/metadata"
cp "${WORK}/connection.json" "${WORK}/pkg/connection.json"
cat > "${WORK}/pkg/metadata/metadata.json" <<EOF
{
  "type": "ccaas",
  "label": "${LABEL}"
}
EOF

# code.tar.gz 顶层包含 connection.json 与 metadata/
tar -C "${WORK}/pkg" -czf "${WORK}/code.tar.gz" connection.json metadata
tar -C "${WORK}" -czf "${OUT}" metadata.json code.tar.gz
echo "已生成 external 链码包：${OUT}（address=${ADDR}）"
