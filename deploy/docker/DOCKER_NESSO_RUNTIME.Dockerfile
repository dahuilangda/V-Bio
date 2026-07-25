FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG NESSO_REPOSITORY=https://github.com/recursionpharma/nesso.git
ARG NESSO_COMMIT=8001d5e1e18b2d1f8ee0d6d56bf39072d9249ac1
ARG TORCH_VERSION=2.5.1
ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cu124

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        git \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        python3 \
        python3-dev \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/local/bin/python \
    && ln -sf /usr/bin/pip3 /usr/local/bin/pip \
    && python -m pip install --upgrade pip setuptools wheel

RUN python -m pip install \
    --index-url "${PYTORCH_INDEX_URL}" \
    "torch==${TORCH_VERSION}"

RUN git init /opt/nesso \
    && git -C /opt/nesso remote add origin "${NESSO_REPOSITORY}" \
    && git -C /opt/nesso fetch --depth 1 origin "${NESSO_COMMIT}" \
    && git -C /opt/nesso checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/nesso rev-parse HEAD)" = "${NESSO_COMMIT}" \
    && python -m pip install /opt/nesso \
    && nesso --help >/dev/null

LABEL org.opencontainers.image.source="https://github.com/recursionpharma/nesso" \
      org.opencontainers.image.revision="${NESSO_COMMIT}" \
      org.opencontainers.image.version="1.0.0"

WORKDIR /workspace

CMD ["nesso", "--help"]
