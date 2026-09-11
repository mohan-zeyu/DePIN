#!/usr/bin/env bash
# up.sh — 启动 depin stage-1 最小网络并部署链码 depincc。
# 步骤：cryptogen → configtxgen → docker compose up → 等待健康 →
#       通道创建/加入 → 链码 package/install/approve/commit → Init 冒烟验证。
# 任一步失败即停止（set -euo pipefail），不假成功。
set -euo pipefail

NETWORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${NETWORK_DIR}/.." && pwd)"
COMPOSE_FILE="${NETWORK_DIR}/docker-compose.yaml"
ORDERER_CONTAINER="orderer.depin.dev"
CHANNEL_NAME="depin-channel"
CHAINCODE_NAME="depincc"
CHAINCODE_VERSION="1.0"
CHAINCODE_SEQUENCE="1"
CHAINCODE_LABEL="${CHAINCODE_NAME}_${CHAINCODE_VERSION}"
CHAINCODE_SRC="${NETWORK_DIR}/../chaincode/depin"   # 挂载到 cli 容器 /opt/chaincode/depin
CRYPTO_DIR="${NETWORK_DIR}/organizations"
ARTIFACTS_DIR="${NETWORK_DIR}/channel-artifacts"
HEALTH_TIMEOUT_SECS="${HEALTH_TIMEOUT_SECS:-120}"

# compose 命令兼容 docker compose v2 与 docker-compose v1。
compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "${COMPOSE_FILE}" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -f "${COMPOSE_FILE}" "$@"
  else
    echo "错误：未找到 docker compose（需要 Docker Compose v2 或 docker-compose v1）。" >&2
    exit 1
  fi
}

# 在 cli 容器内以 UserOrg Admin 身份执行命令。
cli() {
  compose exec -T cli "$@"
}

# 在 cli 容器内执行 bash 片段。
cli_bash() {
  compose exec -T cli bash -c "$1"
}

wait_healthy() {
  local name="$1" url="$2" waited=0
  echo "==> 等待 ${name} 健康 (${url})"
  while ! curl -sf --max-time 3 "${url}" >/dev/null 2>&1; do
    if [ "${waited}" -ge "${HEALTH_TIMEOUT_SECS}" ]; then
      echo "错误：等待 ${name} 健康超时（${HEALTH_TIMEOUT_SECS}s）。查看日志：docker compose -f ${COMPOSE_FILE} logs ${name}" >&2
      exit 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "==> ${name} 健康"
}

# ---- 前置检查 ----
for dep in curl docker; do
  if ! command -v "${dep}" >/dev/null 2>&1; then
    echo "错误：缺少依赖 ${dep}。" >&2
    exit 1
  fi
done
for bin in cryptogen configtxgen; do
  if [ ! -x "${NETWORK_DIR}/bin/${bin}" ]; then
    echo "错误：缺少 ${NETWORK_DIR}/bin/${bin}，请先运行 network/scripts/bootstrap.sh" >&2
    exit 1
  fi
done
if [ ! -f "${CHAINCODE_SRC}/go.mod" ]; then
  echo "错误：未找到链码源码 ${CHAINCODE_SRC}（期望 chaincode/depin 为 Go 模块）。" >&2
  exit 1
fi

cd "${NETWORK_DIR}"
export FABRIC_CFG_PATH="${NETWORK_DIR}"

# ---- 1. cryptogen：生成全部 8 个组织的 MSP 与身份 ----
echo "==> cryptogen 生成证书（输出 ${CRYPTO_DIR}）"
rm -rf "${CRYPTO_DIR}"
"${NETWORK_DIR}/bin/cryptogen" generate --config="${NETWORK_DIR}/crypto-config.yaml" --output="${CRYPTO_DIR}"

# UserOrg 额外身份：把 User3 复制为 worker1（Worker 运行身份，仍在 UserOrg）。
USER_DIR="${CRYPTO_DIR}/peerOrganizations/user.depin.dev/users"
if [ ! -d "${USER_DIR}/worker1@user.depin.dev" ]; then
  cp -R "${USER_DIR}/User3@user.depin.dev" "${USER_DIR}/worker1@user.depin.dev"
  echo "==> UserOrg 身份：user1=User1, user2=User2, worker1=User3（复制目录）"
fi

# ---- 2. configtxgen：genesis block + 通道创建交易 ----
echo "==> configtxgen 生成通道工件（输出 ${ARTIFACTS_DIR}）"
rm -rf "${ARTIFACTS_DIR}"
mkdir -p "${ARTIFACTS_DIR}"
"${NETWORK_DIR}/bin/configtxgen" -profile DepinOrdererGenesis \
  -channelID orderersystem -outputBlock "${ARTIFACTS_DIR}/genesis.block"
"${NETWORK_DIR}/bin/configtxgen" -profile DepinChannel \
  -channelID "${CHANNEL_NAME}" -outputCreateChannelTx "${ARTIFACTS_DIR}/${CHANNEL_NAME}.tx"

# ---- 3. 启动容器并等待健康 ----
echo "==> docker compose up -d（orderer/peer/cli；depin-cc 在链码 install 后启动）"
compose up -d orderer.depin.dev peer0.user.depin.dev cli
wait_healthy orderer.depin.dev "http://localhost:9443/healthz"
wait_healthy peer0.user.depin.dev "http://localhost:9444/healthz"

ORDERER_CA="/opt/organizations/ordererOrganizations/depin.dev/orderers/orderer.depin.dev/tls/ca.crt"
PEER_TLS_ROOTCERT="/opt/organizations/peerOrganizations/user.depin.dev/peers/peer0.user.depin.dev/tls/ca.crt"

# ---- 4. 创建并加入通道（UserOrg Admin 执行） ----
# ---- 4. 创建通道并加入 ----
# 单节点 Raft 在 operations 健康后仍需数秒完成选举；未选出 leader 时
# 通道创建报 SERVICE_UNAVAILABLE，属时序而非配置错误。
# 注意不能用 peer channel fetch 探测系统通道：cli 的 UserOrg 身份会得到 FORBIDDEN。
wait_raft_leader() {
  local waited=0
  echo "==> 等待 orderer Raft leader 选举完成"
  until docker logs "${ORDERER_CONTAINER}" 2>&1 | grep -q "Start accepting requests as Raft leader"; do
    if [ "${waited}" -ge "${HEALTH_TIMEOUT_SECS}" ]; then
      echo "错误：等待 Raft leader 超时（${HEALTH_TIMEOUT_SECS}s）。" >&2
      exit 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "==> Raft leader 就绪"
}
wait_raft_leader

echo "==> 创建通道 ${CHANNEL_NAME} 并加入 peer0"
cli peer channel create \
  -o orderer.depin.dev:7050 -c "${CHANNEL_NAME}" \
  -f "/opt/channel-artifacts/${CHANNEL_NAME}.tx" \
  --tls --cafile "${ORDERER_CA}" \
  --outputBlock "/opt/channel-artifacts/${CHANNEL_NAME}.block"
cli peer channel join -b "/opt/channel-artifacts/${CHANNEL_NAME}.block"
cli peer channel list

# ---- 5. 链码 CCAAS 构建：本机交叉编译 → 专用镜像 ----
# fabric 2.5 内置 docker client（API 1.25）与 Docker 29+/OrbStack 不兼容，
# peer 无法在 install 时构建 golang 链码镜像，因此采用 CCAAS：链码独立容器常驻。
ARCH="$(docker version --format '{{.Server.Arch}}' 2>/dev/null || uname -m)"
echo "==> 交叉编译链码（linux/${ARCH}）"
mkdir -p "${REPO_ROOT}/network/build"
(cd "${REPO_ROOT}/chaincode/depin" && \
  CGO_ENABLED=0 GOOS=linux GOARCH="${ARCH}" go build -o "${REPO_ROOT}/network/build/chaincode-linux" .) \
  || { echo "错误：链码交叉编译失败。" >&2; exit 1; }
docker build -q -t depin-cc:1.0 -f "${NETWORK_DIR}/ccaas/Dockerfile" "${REPO_ROOT}" \
  || { echo "错误：构建 depin-cc 镜像失败。" >&2; exit 1; }

# ---- 6. 链码 lifecycle 部署：ccaas package → install → 启动 cc 服务 → approve → commit ----
echo "==> 部署链码 ${CHAINCODE_NAME} v${CHAINCODE_VERSION}（CCAAS）"
"${NETWORK_DIR}/scripts/package-ccaas.sh" "${ARTIFACTS_DIR}/${CHAINCODE_LABEL}-ccaas.tar.gz" "depin-cc:9999"
cli peer lifecycle chaincode install "/opt/channel-artifacts/${CHAINCODE_LABEL}-ccaas.tar.gz"

PACKAGE_ID="$(cli_bash "peer lifecycle chaincode queryinstalled --output json" \
  | tr -d '\n\t' | grep -oE '"package_id": ?"[^"]*"' | head -1 | sed 's/.*"package_id": *"//; s/"//')"
if [ -z "${PACKAGE_ID}" ]; then
  echo "错误：未取得链码 Package ID（queryinstalled 无 ${CHAINCODE_LABEL}）。" >&2
  exit 1
fi
echo "==> Package ID: ${PACKAGE_ID}"

# CCAAS：以实际 package-id 启动链码服务容器（compose 读取 CC_PACKAGE_ID）
export CC_PACKAGE_ID="${PACKAGE_ID}"
compose up -d depin-cc
sleep 2
if ! docker ps --format '{{.Names}}' | grep -q '^depin-cc$'; then
  echo "错误：depin-cc 容器未运行（docker logs depin-cc 查看）。" >&2
  exit 1
fi

cli peer lifecycle chaincode approveformyorg \
  -o orderer.depin.dev:7050 --tls --cafile "${ORDERER_CA}" \
  --channelID "${CHANNEL_NAME}" --name "${CHAINCODE_NAME}" \
  --version "${CHAINCODE_VERSION}" --package-id "${PACKAGE_ID}" \
  --sequence "${CHAINCODE_SEQUENCE}"

# 等待 approve 生效后再 commit（提交前确认 readiness，失败即停）。
for i in $(seq 1 30); do
  if cli_bash "peer lifecycle chaincode checkcommitreadiness \
      --channelID ${CHANNEL_NAME} --name ${CHAINCODE_NAME} \
      --version ${CHAINCODE_VERSION} --sequence ${CHAINCODE_SEQUENCE} \
      --output json" | tr -d ' \n\t' | grep -q '"UserOrg":true'; then
    break
  fi
  if [ "${i}" -eq 30 ]; then
    echo "错误：checkcommitreadiness 中 UserOrg 未批准。" >&2
    exit 1
  fi
  sleep 2
done

cli peer lifecycle chaincode commit \
  -o orderer.depin.dev:7050 --tls --cafile "${ORDERER_CA}" \
  --channelID "${CHANNEL_NAME}" --name "${CHAINCODE_NAME}" \
  --version "${CHAINCODE_VERSION}" --sequence "${CHAINCODE_SEQUENCE}" \
  --peerAddresses peer0.user.depin.dev:7051 \
  --tlsRootCertFiles "${PEER_TLS_ROOTCERT}"

cli peer lifecycle chaincode querycommitted --channelID "${CHANNEL_NAME}"

# ---- 7. 调用 Init 写入网络元数据，并做冒烟查询 ----
echo "==> 调用 ${CHAINCODE_NAME}:Init 与 GetPolicy 冒烟验证"
cli peer chaincode invoke \
  -o orderer.depin.dev:7050 --tls --cafile "${ORDERER_CA}" \
  -C "${CHANNEL_NAME}" -n "${CHAINCODE_NAME}" \
  --peerAddresses peer0.user.depin.dev:7051 \
  --tlsRootCertFiles "${PEER_TLS_ROOTCERT}" \
  -c '{"function":"depin:Init","Args":[]}'

cli peer chaincode query -C "${CHANNEL_NAME}" -n "${CHAINCODE_NAME}" \
  -c '{"function":"depin:GetPolicy","Args":[]}'

echo
echo "==> 网络就绪：通道 ${CHANNEL_NAME}，链码 ${CHAINCODE_NAME} 已提交并初始化。"
echo "    冒烟（WhoAmI，UserOrg Admin）："
echo "    docker compose -f ${COMPOSE_FILE} exec cli peer chaincode query -C ${CHANNEL_NAME} -n ${CHAINCODE_NAME} -c '{\"function\":\"depin:WhoAmI\",\"Args\":[]}'"
echo "    停止：network/scripts/down.sh"
