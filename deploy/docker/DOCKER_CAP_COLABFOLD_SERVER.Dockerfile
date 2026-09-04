FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG http_proxy
ARG https_proxy
ARG no_proxy
ARG APT_MIRROR=
ARG GO_VERSION=1.21.5
ARG GO_DOWNLOAD_URL=
ARG GO_MIRROR=https://mirrors.aliyun.com/golang
ARG GO_MODULE_PROXY=https://goproxy.cn,direct
ARG GO_SUMDB=off
ARG MMSEQS_DOWNLOAD_URL=https://mmseqs.com/latest/mmseqs-linux-gpu.tar.gz
# mmseqs.com 常需代理而 apt/go 镜像直连更快，故单独给下载步骤配代理而不是全局 ENV
ARG MMSEQS_DOWNLOAD_PROXY=
ARG BACKEND_COMMIT=14e087560f309f989a5e1feb54fd1f9c988076d5

ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY} \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    no_proxy=${no_proxy}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN if [[ -n "${APT_MIRROR}" ]]; then \
      sed -i "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi && \
    apt-get update && apt-get install -y \
    curl \
    git \
    aria2 \
    rsync \
    build-essential \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    go_tarball="go${GO_VERSION}.linux-amd64.tar.gz"; \
    go_archive="/tmp/${go_tarball}"; \
    downloaded=0; \
    for base in "${GO_DOWNLOAD_URL}" "https://go.dev/dl" "${GO_MIRROR}"; do \
      [[ -n "${base}" ]] || continue; \
      url="${base%/}/${go_tarball}"; \
      echo "Trying Go download from: ${url}"; \
      if curl -fL --connect-timeout 20 --max-time 1200 --retry 3 --retry-delay 2 "${url}" -o "${go_archive}"; then \
        downloaded=1; \
        break; \
      fi; \
    done; \
    if [[ "${downloaded}" -ne 1 ]]; then \
      echo "Failed to download ${go_tarball} from all mirrors."; \
      exit 1; \
    fi; \
    rm -rf /usr/local/go && \
    tar -C /usr/local -xzf "${go_archive}" && \
    rm -f "${go_archive}"

ENV PATH="/usr/local/go/bin:${PATH}"

WORKDIR /app

# 预置产物目录（可选）:放入 mmseqs-linux-gpu.tar.gz 后构建不再依赖外网。
# 内容不入 git,目录本身用 .gitkeep 占位。
COPY capabilities/colabfold_server/_prebuilt/ /tmp/mmseqs_prebuilt/

RUN set -eux; \
    install_mmseqs_binary() { \
      local url="$1"; \
      local target="$2"; \
      local archive="/tmp/mmseqs-linux-gpu.tar.gz"; \
      local extract_dir="/tmp/mmseqs_extract_$(basename "$target")"; \
      rm -rf "${extract_dir}" && mkdir -p "${extract_dir}"; \
      if [[ -s "/tmp/mmseqs_prebuilt/mmseqs.real" ]]; then \
        echo "Using pre-seeded MMseqs binary (pinned to the version that built the local GPU indexes)"; \
        install -Dm755 "/tmp/mmseqs_prebuilt/mmseqs.real" "${target}"; \
        rm -rf "${extract_dir}"; \
        return 0; \
      fi; \
      if [[ -s "/tmp/mmseqs_prebuilt/mmseqs-linux-gpu.tar.gz" ]]; then \
        echo "Using pre-seeded MMseqs archive from build context"; \
        cp /tmp/mmseqs_prebuilt/mmseqs-linux-gpu.tar.gz "${archive}"; \
      else \
        echo "Downloading MMseqs from: ${url}"; \
        proxy_args=""; \
        if [[ -n "${MMSEQS_DOWNLOAD_PROXY}" ]]; then proxy_args=(-x "${MMSEQS_DOWNLOAD_PROXY}" --http1.1); fi; \
        if ! curl -fL "${proxy_args[@]}" --connect-timeout 20 --max-time 1200 --retry 3 --retry-delay 2 "${url}" -o "${archive}"; then \
          echo "Failed to download MMseqs archive: ${url}"; \
          exit 1; \
        fi; \
      fi; \
      if ! tar -tzf "${archive}" >/dev/null 2>&1; then \
        echo "Downloaded file is not a valid MMseqs archive: ${url}"; \
        exit 1; \
      fi; \
      tar -xzf "${archive}" -C "${extract_dir}"; \
      mmseqs_bin="$(find "${extract_dir}" -type f -name mmseqs | head -n 1)"; \
      if [[ -z "${mmseqs_bin}" ]]; then \
        echo "MMseqs binary not found in archive: ${url}"; \
        exit 1; \
      fi; \
      install -Dm755 "${mmseqs_bin}" "${target}"; \
      rm -rf "${extract_dir}" "${archive}"; \
    }; \
    install_mmseqs_binary "${MMSEQS_DOWNLOAD_URL}" "/app/mmseqs/bin/mmseqs.real"

RUN cat <<'EOF' > /app/mmseqs/bin/mmseqs
#!/bin/bash
mode="${MMSEQS_LOAD_MODE:-}"
if [[ -n "$mode" && ! "$mode" =~ ^[0-9]+$ ]]; then
  mode=""
fi

real_bin="${MMSEQS_REAL_BIN:-/app/mmseqs/bin/mmseqs.real}"
if [[ ! -x "$real_bin" ]]; then
  echo "No usable MMseqs binary found at: ${real_bin}" >&2
  exit 1
fi

cmd="$1"
if [[ -z "$cmd" || "$cmd" == "version" || "$cmd" == "help" || "$cmd" == "list" ]]; then
  exec "$real_bin" "$@"
fi

shift
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

exec "$real_bin" "$@"
EOF

RUN chmod +x /app/mmseqs/bin/mmseqs

ENV PATH="/app/mmseqs/bin:${PATH}"

# V-Bio fork of MMseqs2-App (vendored at capabilities/colabfold_server/mmseqs-server):
# adds DELETE /ticket/{id} cancellation (backend/cancel.go) — upstream has no way to
# abandon a search, so a client timeout strands a full GPU until the search finishes.
COPY capabilities/colabfold_server/mmseqs-server/backend /app/mmseqs-server-build/backend
RUN cd /app/mmseqs-server-build/backend && \
    GOPROXY="${GO_MODULE_PROXY}" GOSUMDB="${GO_SUMDB}" go build -o /app/msa-server

# --- GPU 租约包装器 ----------------------------------------------------------
# 目标库为 *_gpu 填充库时,search 先从 celery 共享 GPU 池(redis, gpu_manager 协议)
# 租卡再以 --gpu 1 运行;纯 bash 实现,无新增镜像依赖。DB 填充/索引一次性命令:
#   mmseqs makepaddedseqdb <seqDB> <seqDB>_gpu && mmseqs createindex <seqDB>_gpu tmp --index-subset 2
COPY capabilities/colabfold_server/mmseqs_gpu_wrapper.sh /tmp/mmseqs_wrapper_new
RUN mv /tmp/mmseqs_wrapper_new /app/mmseqs/bin/mmseqs && chmod +x /app/mmseqs/bin/mmseqs

COPY capabilities/colabfold_server/start_debug.sh /app/start.sh
COPY capabilities/colabfold_server/prepare_databases.sh /app/prepare_databases.sh

RUN chmod +x /app/start.sh /app/prepare_databases.sh && \
    mkdir -p /app/tmp /app/databases /app/jobs && \
    chmod 1777 /app/tmp

EXPOSE 8080

CMD ["/app/start.sh"]
