# depin 仓库根 Makefile
# 常用目标：make bootstrap && make up && make test && make down

.PHONY: bootstrap up down test test-chaincode test-backend test-worker test-experiments \
        test-integration test-gpu fmt vet clean

## bootstrap: 下载 fabric 2.5.10 二进制（按平台）并拉取 docker 镜像（幂等）
bootstrap:
	./network/scripts/bootstrap.sh

## up: 生成证书/通道工件，启动网络并部署链码 depincc（详见 network/README.md）
up:
	./network/scripts/up.sh

## down: 停止网络并清理全部生成物
down:
	./network/scripts/down.sh

## test: 无 GPU 的逻辑测试（链码 + 后端；worker/ 若有 pytest 测试则一并执行）
test: test-chaincode test-backend test-worker test-experiments

## test-chaincode: 链码构建 + vet + 单元测试（身份隔离用例）
test-chaincode:
	cd chaincode/depin && go build ./... && go vet ./... && go test ./...

## test-backend: 后端构建 + vet + 单元测试（mock 链码客户端，不依赖网络）
test-backend:
	cd backend && go build ./... && go vet ./... && go test ./...

## test-worker: worker/ 与 experiments/ 的纯逻辑测试（unittest，无 GPU 依赖）
test-worker:
	@if [ -n "$$(ls worker/tests/test_*.py 2>/dev/null)" ]; then \
		echo "运行 worker Python 测试"; \
		cd worker && python3 -m unittest discover -s tests && cd "$(CURDIR)"; \
	else \
		echo "worker/ 暂无 Python 测试，跳过"; \
	fi

## test-integration: tier2 Fabric 集成（需要 docker）：起网→部署链码→冒烟→清理
test-integration:
	./network/scripts/up.sh
	docker compose -f network/docker-compose.yaml exec cli peer chaincode query \
		-C depin-channel -n depincc -c '{"function":"depin:WhoAmI","Args":[]}'
	./network/scripts/down.sh

## test-gpu: tier3 GPU 实验（需要真实 NVIDIA GPU 环境）
test-gpu:
	@if [ -f experiments/run_experiment.py ]; then \
		cd experiments && python3 run_experiment.py; \
	else \
		echo "需 GPU 环境：experiments/run_experiment.py 不存在（tier3 未实现/未配置）"; \
		exit 1; \
	fi

## fmt: Go 两个 module 的 gofmt 检查（列出差异，不自动改写）
fmt:
	@cd chaincode/depin && gofmt -l .
	@cd backend && gofmt -l .

## vet: Go 两个 module 的 go vet
vet:
	cd chaincode/depin && go vet ./...
	cd backend && go vet ./...

## test-experiments: experiments/ 纯逻辑测试（ncu 输出解析、报告汇总；无 GPU 依赖）
test-experiments:
	@if [ -n "$$(ls experiments/tests/test_*.py 2>/dev/null)" ]; then \
		echo "运行 experiments Python 测试"; \
		cd experiments && python3 -m unittest discover -s tests && cd "$(CURDIR)"; \
	else \
		echo "experiments/ 暂无 Python 测试，跳过"; \
	fi

## clean: 清理本地构建产物（网络生成物用 make down 清理）
clean:
	cd chaincode/depin && go clean -testcache
	cd backend && go clean -testcache
