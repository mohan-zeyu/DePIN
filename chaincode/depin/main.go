// package main 是链码 depincc 的入口。合约命名空间为 "depin"
// （通过嵌入的 contractapi.Contract.Name 设置），对客户端表现为 depin.DepinContract。
//
// CCAAS 模式：contractapi v2 的 Start() 检测到 CHAINCODE_SERVER_ADDRESS +
// CORE_CHAINCODE_ID_NAME 环境变量时自动以 server 方式监听（stage-1 用此模式
// 绕开 fabric 2.5 内置 docker client 与 Docker 29+/OrbStack 构建不兼容的问题）。
package main

import (
	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"
)

func main() {
	contract := &DepinContract{}
	contract.Name = "depin"
	cc, err := contractapi.NewChaincode(contract)
	if err != nil {
		panic(err)
	}
	if err := cc.Start(); err != nil {
		panic(err)
	}
}
