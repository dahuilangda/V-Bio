# Mol-1 结构预测引擎镜像
# 构建（HTTP_PROXY 为构建期拉取 pip/apt 的代理，不影响运行时）:
#   docker build --build-arg HTTP_PROXY=http://172.16.34.31:2080 \
#     --build-arg HTTPS_PROXY=http://172.16.34.31:2080 \
#     -t vbio-mol1-runtime:1.0.0 .
# 说明: 基础镜像为 runtime 版（无 nvcc），fused LayerNorm 扩展不可编译，
# 引擎脚本以 LAYERNORM_TYPE=torch 运行 PyTorch 实现（与 boltz2score 的
# --no_kernels 同理，24GB 卡上更稳）。
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ARG HTTP_PROXY
ARG HTTPS_PROXY
ENV DEBIAN_FRONTEND=noninteractive \
    HTTP_PROXY=${HTTP_PROXY} HTTPS_PROXY=${HTTPS_PROXY} \
    http_proxy=${HTTP_PROXY} https_proxy=${HTTPS_PROXY}

RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common git curl ca-certificates gcc \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3.12-dev \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python3.12

# Mol-1 源码随镜像固定（Apache-2.0，见 Mol-1-source/LICENSE）
COPY Mol-1-source /workspace/Mol-1
RUN python3.12 -m pip install --no-cache-dir "/workspace/Mol-1[cuda12]"

ENV HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy= \
    MOL_ROOT_DIR=/cache LAYERNORM_TYPE=torch

WORKDIR /workspace
CMD ["python3.12"]
