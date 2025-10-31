# 使用一个基础的 Linux 镜像
FROM ubuntu:22.04

# 设置环境变量，避免交互式提示
ENV DEBIAN_FRONTEND=noninteractive

# 可选的代理配置（由构建参数传入）
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG http_proxy
ARG https_proxy
ARG no_proxy

ENV HTTP_PROXY=$HTTP_PROXY \
    HTTPS_PROXY=$HTTPS_PROXY \
    NO_PROXY=$NO_PROXY \
    http_proxy=$http_proxy \
    https_proxy=$https_proxy \
    no_proxy=$no_proxy

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# 从项目 .env 中提取代理配置 (如果存在)
COPY .env /tmp/build.env
RUN touch /tmp/proxy.sh && \
    if [ -f /tmp/build.env ]; then \
        while IFS='=' read -r raw_key raw_value; do \
            [[ -z "$raw_key" ]] && continue; \
            [[ "$raw_key" =~ ^# ]] && continue; \
            key="${raw_key//[[:space:]]/}"; \
            value="${raw_value%%#*}"; \
            value="${value//$'\r'/}"; \
            value="${value//$'\n'/}"; \
            value="${value%\"}"; value="${value%\'}"; \
            value="${value#\"}"; value="${value#\'}"; \
            value=$(echo "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'); \
            case "$key" in \
                HTTP_PROXY|http_proxy|HTTPS_PROXY|https_proxy|NO_PROXY|no_proxy) \
                    upper=$(echo "$key" | tr '[:lower:]' '[:upper:]'); \
                    lower=$(echo "$key" | tr '[:upper:]' '[:lower:]'); \
                    echo "export ${upper}=\"${value}\"" >> /tmp/proxy.sh; \
                    if [ "$upper" != "$lower" ]; then \
                        echo "export ${lower}=\"${value}\"" >> /tmp/proxy.sh; \
                    fi; \
                    ;; \
            esac; \
        done < /tmp/build.env; \
        rm -f /tmp/build.env; \
    fi && \
    chmod +x /tmp/proxy.sh

# 设置脚本中定义的版本号
ARG MMSEQS_COMMIT=45e2839d83a913d0fde50b91f72e2e5c5c685b89
ARG BACKEND_COMMIT=14e087560f309f989a5e1feb54fd1f9c988076d5

# 1. 安装官方脚本所需的所有依赖 (修正了软件包名称)
# "aria2c" 对应的包是 "aria2"
RUN if [ -f /tmp/proxy.sh ]; then source /tmp/proxy.sh; fi; \
    apt-get update && apt-get install -y \
    curl \
    git \
    aria2 \
    rsync \
    build-essential \
    wget \
    && rm -rf /var/lib/apt/lists/*

# 2. 安装 Go 语言环境 (采用更可靠的官方二进制包方式)
RUN if [ -f /tmp/proxy.sh ]; then source /tmp/proxy.sh; fi; \
    wget https://go.dev/dl/go1.21.5.linux-amd64.tar.gz && \
    rm -rf /usr/local/go && \
    tar -C /usr/local -xzf go1.21.5.linux-amd64.tar.gz && \
    rm go1.21.5.linux-amd64.tar.gz
ENV PATH="/usr/local/go/bin:${PATH}"

# 设置工作目录
WORKDIR /app

# 3. 下载脚本中指定的 MMseqs 版本
# RUN curl -s -o- https://mmseqs.com/archive/${MMSEQS_COMMIT}/mmseqs-linux-avx2.tar.gz | tar -xzf - mmseqs/bin/mmseqs
RUN if [ -f /tmp/proxy.sh ]; then source /tmp/proxy.sh; fi; \
    curl -s -L https://mmseqs.com/latest/mmseqs-linux-avx2.tar.gz | tar -xzf - mmseqs/bin/mmseqs
RUN mv /app/mmseqs/bin/mmseqs /app/mmseqs/bin/mmseqs.real && \
    cat <<'EOF' > /app/mmseqs/bin/mmseqs
#!/bin/bash
mode="${MMSEQS_LOAD_MODE:-}"
if [[ -n "$mode" && ! "$mode" =~ ^[0-9]+$ ]]; then
  mode=""
fi

cmd="$1"
if [[ -z "$cmd" || "$cmd" == "version" || "$cmd" == "help" || "$cmd" == "list" ]]; then
  exec /app/mmseqs/bin/mmseqs.real "$@"
fi

shift

# 先删除原参数中的 --db-load-mode（及其值）
sanitized_args=()
skip_next=0
for arg in "$@"; do
  if [[ "$skip_next" = 1 ]]; then
    skip_next=0
    continue
  fi
  if [[ "$arg" == "--db-load-mode" ]]; then
    skip_next=1
    continue
  fi
  sanitized_args+=("$arg")
done

case "$cmd" in
  search|expandaln|filterresult|result2msa|result2profile|align|convertalis|prefilter)
    if [[ -n "$mode" ]]; then
      set -- "$cmd" --db-load-mode "$mode" "${sanitized_args[@]}"
    else
      set -- "$cmd" "${sanitized_args[@]}"
    fi
    ;;
  *)
    set -- "$cmd" "${sanitized_args[@]}"
    ;;
esac

exec /app/mmseqs/bin/mmseqs.real "$@"
EOF
RUN chmod +x /app/mmseqs/bin/mmseqs
ENV PATH="/app/mmseqs/bin:${PATH}"

# 4. 下载并编译真正的服务器源代码 (MMseqs2-App)
RUN if [ -f /tmp/proxy.sh ]; then source /tmp/proxy.sh; fi; \
    git clone https://github.com/soedinglab/MMseqs2-App.git mmseqs-server
RUN if [ -f /tmp/proxy.sh ]; then source /tmp/proxy.sh; fi; \
    cd mmseqs-server && git checkout ${BACKEND_COMMIT}
RUN if [ -f /tmp/proxy.sh ]; then source /tmp/proxy.sh; fi; \
    cd mmseqs-server/backend && go build -o /app/msa-server

RUN rm -f /tmp/proxy.sh

# 复制我们自己的启动脚本
COPY start_debug.sh /app/start.sh
RUN chmod +x /app/start.sh

# 预创建运行时需要写入的目录并放宽权限，方便以非 root 用户运行
RUN mkdir -p /app/tmp && chmod 1777 /app/tmp

# 暴露端口
EXPOSE 8080

# 定义容器启动时执行的命令
CMD ["/app/start.sh"]
