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

RUN set -eux; \
    install_mmseqs_binary() { \
      local url="$1"; \
      local target="$2"; \
      local archive="/tmp/$(basename "$url")"; \
      local extract_dir="/tmp/mmseqs_extract_$(basename "$target")"; \
      echo "Downloading MMseqs from: ${url}"; \
      if ! curl -fL --connect-timeout 20 --max-time 1200 --retry 3 --retry-delay 2 "${url}" -o "${archive}"; then \
        echo "Failed to download MMseqs archive: ${url}"; \
        exit 1; \
      fi; \
      if ! tar -tzf "${archive}" >/dev/null 2>&1; then \
        echo "Downloaded file is not a valid MMseqs archive: ${url}"; \
        exit 1; \
      fi; \
      rm -rf "${extract_dir}" && mkdir -p "${extract_dir}"; \
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

RUN git clone https://github.com/soedinglab/MMseqs2-App.git mmseqs-server && \
    cd mmseqs-server && \
    git checkout "${BACKEND_COMMIT}" && \
    cd backend && \
    GOPROXY="${GO_MODULE_PROXY}" GOSUMDB="${GO_SUMDB}" go build -o /app/msa-server

COPY capabilities/colabfold_server/start_debug.sh /app/start.sh
COPY capabilities/colabfold_server/prepare_databases.sh /app/prepare_databases.sh

RUN chmod +x /app/start.sh /app/prepare_databases.sh && \
    mkdir -p /app/tmp /app/databases /app/jobs && \
    chmod 1777 /app/tmp

EXPOSE 8080

CMD ["/app/start.sh"]
