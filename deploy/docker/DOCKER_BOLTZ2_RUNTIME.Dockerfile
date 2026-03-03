FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    ca-certificates \
    git \
    bash \
    build-essential \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/local/bin/python && \
    ln -sf /usr/bin/pip3 /usr/local/bin/pip

RUN python -m pip install --upgrade pip setuptools wheel

# Unified runtime image for:
# - Boltz2 prediction (backend/runtime/boltz_wrapper.py)
# - Boltz2Score scoring (capabilities/boltz2score/boltz2score.py)
# - Affinity pipeline relying on Boltz runtime
RUN python -m pip install \
    "boltz[cuda]" \
    torch \
    rdkit \
    gemmi \
    numpy \
    pandas \
    scipy \
    biopython \
    pyyaml \
    requests \
    redis \
    tqdm \
    pytorch-lightning \
    click

WORKDIR /workspace/vbio

CMD ["python", "-c", "import boltz; print('boltz runtime ready')"]
