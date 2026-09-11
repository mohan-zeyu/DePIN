# CCAAS 链码服务镜像：构建上下文 = 仓库根目录
# 二进制 network/build/chaincode-linux-amd64 由 scripts/up.sh 交叉编译生成，
# 镜像构建只做拷贝，不依赖容器内网络。
FROM hyperledger/fabric-baseos:2.5.10
COPY network/build/chaincode-linux /usr/local/bin/depincc
ENV PATH=/usr/local/bin:$PATH
EXPOSE 9999
CMD ["depincc"]
