// gateway.go — fabric.Client 的 fabric-gateway 实现（连接 peer0.user.depin.dev）。
// 构造时不主动拨号（grpc.NewClient 惰性连接），因此单元测试与服务启动
// 不依赖真实网络；连接错误只在具体 Query/Submit 时返回。
package fabric

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"
	"time"

	"github.com/hyperledger/fabric-gateway/pkg/client"
	"github.com/hyperledger/fabric-gateway/pkg/identity"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
)

// GatewayClient 实现 Client 接口。
type GatewayClient struct {
	conn     *grpc.ClientConn
	gw       *client.Gateway
	contract *client.Contract
}

// NewGatewayClient 用文件钱包身份建立 gateway 连接并绑定通道/链码。
func NewGatewayClient(cfg Config, walletCertPEM, keyPEM []byte, mspID string) (*GatewayClient, error) {
	cert, err := identity.CertificateFromPEM(walletCertPEM)
	if err != nil {
		return nil, fmt.Errorf("解析钱包证书失败: %w", err)
	}
	privKey, err := identity.PrivateKeyFromPEM(keyPEM)
	if err != nil {
		return nil, fmt.Errorf("解析钱包私钥失败: %w", err)
	}
	clientIdentity, err := identity.NewX509Identity(mspID, cert)
	if err != nil {
		return nil, fmt.Errorf("构造 X509 身份失败: %w", err)
	}
	sign, err := identity.NewPrivateKeySign(privKey)
	if err != nil {
		return nil, fmt.Errorf("构造签名器失败: %w", err)
	}

	var dialOpts []grpc.DialOption
	if cfg.PeerTLSCACert != "" {
		caPEM, err := os.ReadFile(cfg.PeerTLSCACert)
		if err != nil {
			return nil, fmt.Errorf("读取 peer TLS CA 失败（%s）: %w", cfg.PeerTLSCACert, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caPEM) {
			return nil, fmt.Errorf("peer TLS CA 文件不是合法 PEM: %s", cfg.PeerTLSCACert)
		}
		dialOpts = append(dialOpts, grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{RootCAs: pool})))
	} else {
		// 仅限本地开发（无 TLS 的 peer）。
		dialOpts = append(dialOpts, grpc.WithTransportCredentials(insecure.NewCredentials()))
	}

	conn, err := grpc.NewClient(cfg.PeerEndpoint, dialOpts...)
	if err != nil {
		return nil, fmt.Errorf("建立 grpc 连接失败: %w", err)
	}

	gw, err := client.Connect(clientIdentity,
		client.WithSign(sign),
		client.WithClientConnection(conn),
		client.WithEvaluateTimeout(5*time.Second),
		client.WithSubmitTimeout(30*time.Second),
		client.WithCommitStatusTimeout(10*time.Second),
	)
	if err != nil {
		return nil, fmt.Errorf("连接 fabric gateway 失败: %w", err)
	}

	network := gw.GetNetwork(cfg.ChannelName)
	contract := network.GetContractWithName(cfg.ChaincodeName, cfg.DefaultContract)
	return &GatewayClient{conn: conn, gw: gw, contract: contract}, nil
}

// Close 关闭 gateway 与 gRPC 连接。
func (c *GatewayClient) Close() error {
	err := c.gw.Close()
	if cerr := c.conn.Close(); err == nil {
		err = cerr
	}
	return err
}

func (c *GatewayClient) Query(ctx context.Context, function string, args ...string) ([]byte, error) {
	return c.contract.EvaluateWithContext(ctx, function, client.WithArguments(args...))
}

func (c *GatewayClient) Submit(ctx context.Context, function string, args ...string) ([]byte, error) {
	return c.contract.SubmitWithContext(ctx, function, client.WithArguments(args...))
}
