// Package fabric 封装后端与 Fabric 网络的交互。
// Client 是最小接口，单元测试注入 mock，不依赖真实网络。
package fabric

import (
	"context"
	"os"
)

// Client 是链码客户端接口（fabric-gateway 实现见 gateway.go，测试用 mock 注入）。
// 合约命名空间（depin）在实现构造时绑定，调用方只指定函数与参数。
type Client interface {
	// Query 执行链码查询（Evaluate，不写账本）。
	Query(ctx context.Context, function string, args ...string) ([]byte, error)
	// Submit 提交交易并等待提交完成（Submit）。
	Submit(ctx context.Context, function string, args ...string) ([]byte, error)
}

// Config 是 gateway 连接配置（由环境变量构造，见 ConfigFromEnv）。
type Config struct {
	PeerEndpoint    string // peer host:port，如 localhost:7051
	PeerTLSCACert   string // peer TLS CA 证书 PEM 路径；空表示非 TLS（仅开发）
	WalletPath      string // 文件钱包目录
	IdentityName    string // 钱包内身份名
	ChannelName     string // 通道名
	ChaincodeName   string // 链码名
	DefaultContract string // 链码内合约命名空间（"depin"）
}

// ContractDepin 是链码 depincc 中的业务合约命名空间。
const ContractDepin = "depin"

// ConfigFromEnv 从环境变量构造连接配置（均有开发默认值）。
func ConfigFromEnv() Config {
	return Config{
		PeerEndpoint:    envOr("FABRIC_PEER_ENDPOINT", "localhost:7051"),
		PeerTLSCACert:   os.Getenv("FABRIC_PEER_TLS_CA_CERT"),
		WalletPath:      envOr("DEPIN_WALLET_PATH", "network/organizations/peerOrganizations/user.depin.dev/wallet"),
		IdentityName:    envOr("DEPIN_IDENTITY_NAME", "user1"),
		ChannelName:     envOr("DEPIN_CHANNEL", "depin-channel"),
		ChaincodeName:   envOr("DEPIN_CHAINCODE", "depincc"),
		DefaultContract: ContractDepin,
	}
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
