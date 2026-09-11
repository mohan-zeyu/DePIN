// server — depin 后端 API（stage-1 最小实现）。
//
// 环境变量（均有开发默认值）：
//
//	PORT                    HTTP 监听端口（默认 8080）
//	FABRIC_PEER_ENDPOINT    peer 地址（默认 localhost:7051）
//	FABRIC_PEER_TLS_CA_CERT peer TLS CA 证书 PEM 路径（空 = 非 TLS，仅开发）
//	DEPIN_WALLET_PATH       文件钱包目录（默认 network/organizations/.../wallet）
//	DEPIN_IDENTITY_NAME     钱包内身份名（默认 user1）
//	DEPIN_CHANNEL           通道（默认 depin-channel）
//	DEPIN_CHAINCODE         链码（默认 depincc）
//
// 钱包布局：<wallet>/<身份名>/cert.pem、key.pem、msp-id（msp-id 为单行 MSP ID 文本）。
// 从 cryptogen 输出导出一个身份：
//
//	mkdir -p wallet/user1
//	cp organizations/peerOrganizations/user.depin.dev/users/User1@user.depin.dev/msp/signcerts/*.pem wallet/user1/cert.pem
//	cp organizations/peerOrganizations/user.depin.dev/users/User1@user.depin.dev/msp/keystore/*_sk wallet/user1/key.pem
//	echo -n UserOrg > wallet/user1/msp-id
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/mohan-zeyu/depin/backend/internal/api"
	"github.com/mohan-zeyu/depin/backend/internal/fabric"
	"github.com/mohan-zeyu/depin/backend/internal/identity"
)

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	cfg := fabric.ConfigFromEnv()
	id, err := identity.LoadWalletIdentity(cfg.WalletPath, cfg.IdentityName)
	if err != nil {
		log.Fatalf("加载钱包身份失败（wallet=%s name=%s）: %v\n%s", cfg.WalletPath, cfg.IdentityName, err,
			"提示：钱包布局为 <wallet>/<身份名>/{cert.pem,key.pem,msp-id}，可从 cryptogen 输出导出，见 main.go 顶部注释。")
	}
	keyPEM, err := id.PrivateKeyPEM(cfg.WalletPath)
	if err != nil {
		log.Fatalf("读取钱包私钥失败: %v", err)
	}
	log.Printf("已加载身份 name=%s mspid=%s clientId=%s", id.Name, id.MSPID, id.ClientID())

	// gateway 连接为惰性拨号：peer 暂不可达不阻塞服务启动，错误延迟到请求时返回。
	cc, err := fabric.NewGatewayClient(cfg, id.CertPEM, keyPEM, id.MSPID)
	if err != nil {
		log.Fatalf("初始化 fabric gateway 失败: %v", err)
	}
	defer func() { _ = cc.Close() }()

	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           api.NewRouter(cc, id),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
		<-sig
		log.Printf("收到退出信号，正在关闭…")
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			log.Printf("HTTP 关闭异常: %v", err)
		}
	}()

	log.Printf("depin-backend 监听 :%s（peer=%s channel=%s chaincode=%s contract=%s）",
		port, cfg.PeerEndpoint, cfg.ChannelName, cfg.ChaincodeName, cfg.DefaultContract)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("HTTP 服务退出: %v", err)
	}
}
