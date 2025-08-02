# 使用一个基础的 Linux 镜像
FROM ubuntu:22.04

# 设置环境变量，避免交互式提示
ENV DEBIAN_FRONTEND=noninteractive

# 设置脚本中定义的版本号
ARG MMSEQS_COMMIT=45e2839d83a913d0fde50b91f72e2e5c5c685b89
ARG BACKEND_COMMIT=14e087560f309f989a5e1feb54fd1f9c988076d5

# 1. 安装官方脚本所需的所有依赖 (修正了软件包名称)
# "aria2c" 对应的包是 "aria2"
RUN apt-get update && apt-get install -y \
    curl \
    git \
    aria2 \
    rsync \
    build-essential \
    wget \
    && rm -rf /var/lib/apt/lists/*

# 2. 安装 Go 语言环境 (采用更可靠的官方二进制包方式)
RUN wget https://go.dev/dl/go1.21.5.linux-amd64.tar.gz && \
    rm -rf /usr/local/go && \
    tar -C /usr/local -xzf go1.21.5.linux-amd64.tar.gz && \
    rm go1.21.5.linux-amd64.tar.gz
ENV PATH="/usr/local/go/bin:${PATH}"

# 设置工作目录
WORKDIR /app

# 3. 下载脚本中指定的 MMseqs 版本
# RUN curl -s -o- https://mmseqs.com/archive/${MMSEQS_COMMIT}/mmseqs-linux-avx2.tar.gz | tar -xzf - mmseqs/bin/mmseqs
RUN curl -s -L https://mmseqs.com/latest/mmseqs-linux-avx2.tar.gz | tar -xzf - mmseqs/bin/mmseqs
ENV PATH="/app/mmseqs/bin:${PATH}"

# 4. 下载并编译真正的服务器源代码 (MMseqs2-App)
RUN git clone https://github.com/soedinglab/MMseqs2-App.git mmseqs-server
RUN cd mmseqs-server && git checkout ${BACKEND_COMMIT}
RUN cd mmseqs-server/backend && go build -o /app/msa-server

# 复制我们自己的启动脚本
COPY start_debug.sh /app/start.sh
RUN chmod +x /app/start.sh

# 暴露端口
EXPOSE 8080

# 定义容器启动时执行的命令
CMD ["/app/start.sh"]