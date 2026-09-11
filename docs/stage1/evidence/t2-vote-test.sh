#!/bin/bash
# 身份隔离实测：跨组织投票 / 同 MSP 双证书 / 越权 / 分 evidence 计票
set -u
VORG=/opt/organizations/peerOrganizations
ORDERER_CA=/opt/organizations/ordererOrganizations/depin.dev/orderers/orderer.depin.dev/tls/ca.crt
PEER_TLS=/opt/organizations/peerOrganizations/user.depin.dev/peers/peer0.user.depin.dev/tls/ca.crt

invoke_as() {
  local msp="$1"; local mspid="$2"; shift 2
  docker exec -e CORE_PEER_MSPCONFIGPATH="$msp" -e CORE_PEER_LOCALMSPID="$mspid" depin-cli \
    peer chaincode invoke -C depin-channel -n depincc \
    -o orderer.depin.dev:7050 --tls --cafile "$ORDERER_CA" \
    --peerAddresses peer0.user.depin.dev:7051 --tlsRootCertFiles "$PEER_TLS" \
    -c "$1" --waitForEvent 2>&1 | tail -1
}
query_as() {
  local msp="$1"; local mspid="$2"; shift 2
  docker exec -e CORE_PEER_MSPCONFIGPATH="$msp" -e CORE_PEER_LOCALMSPID="$mspid" depin-cli \
    peer chaincode query -C depin-channel -n depincc -c "$1" 2>&1 | tail -1
}

echo "=== 1. Verify1Org Admin 投票 (order-1,ev-1,v1,approve) → 应成功 ==="
invoke_as "$VORG/verify1.depin.dev/users/Admin@verify1.depin.dev/msp" Verify1Org \
  '{"function":"depin:SubmitVerificationVote","Args":["order-1","evhash-1","v1","approve"]}'

echo "=== 2. Verify1Org User1（同 MSP 第二张证书）投同键 → 应拒绝 ==="
invoke_as "$VORG/verify1.depin.dev/users/User1@verify1.depin.dev/msp" Verify1Org \
  '{"function":"depin:SubmitVerificationVote","Args":["order-1","evhash-1","v1","approve"]}'

echo "=== 3. UserOrg Admin 越权投票 → 应拒绝 ==="
invoke_as "$VORG/user.depin.dev/users/Admin@user.depin.dev/msp" UserOrg \
  '{"function":"depin:SubmitVerificationVote","Args":["order-1","evhash-1","v1","approve"]}'

echo "=== 4. Verify2Org Admin 投同键 → 应成功（第2票） ==="
invoke_as "$VORG/verify2.depin.dev/users/Admin@verify2.depin.dev/msp" Verify2Org \
  '{"function":"depin:SubmitVerificationVote","Args":["order-1","evhash-1","v1","approve"]}'

echo "=== 5. Verify1Org Admin 对不同 evidence (ev-2) 投票 → 应分开计票 ==="
invoke_as "$VORG/verify1.depin.dev/users/Admin@verify1.depin.dev/msp" Verify1Org \
  '{"function":"depin:SubmitVerificationVote","Args":["order-1","evhash-2","v1","approve"]}'

echo "=== 6. 计票 ev-1（应为 2 票） ==="
query_as "$VORG/user.depin.dev/users/Admin@user.depin.dev/msp" UserOrg \
  '{"function":"depin:GetVerificationVotes","Args":["order-1","evhash-1","v1"]}'

echo "=== 7. 计票 ev-2（应为 1 票） ==="
query_as "$VORG/user.depin.dev/users/Admin@user.depin.dev/msp" UserOrg \
  '{"function":"depin:GetVerificationVotes","Args":["order-1","evhash-2","v1"]}'

echo "=== 8. 同一 Admin 重复投完全相同票 → 应幂等成功 ==="
invoke_as "$VORG/verify1.depin.dev/users/Admin@verify1.depin.dev/msp" Verify1Org \
  '{"function":"depin:SubmitVerificationVote","Args":["order-1","evhash-1","v1","approve"]}'
