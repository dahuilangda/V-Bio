FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    CONDA_DIR=/opt/conda \
    PATH=/opt/conda/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    bzip2 \
    ca-certificates \
    curl \
    git \
    gosu \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    wget \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/miniforge.sh \
    https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
    && bash /tmp/miniforge.sh -b -p ${CONDA_DIR} \
    && rm -f /tmp/miniforge.sh

WORKDIR /workspace

COPY environment_cu128_base.yml /tmp/environment_cu128_base.yml

RUN conda env create -f /tmp/environment_cu128_base.yml \
    && conda clean -afy

RUN /bin/bash -lc "source ${CONDA_DIR}/etc/profile.d/conda.sh \
    && conda activate pxm_cu128 \
    && conda install -y -c conda-forge openmm pdbfixer \
    && pip install torch==2.7.0 --index-url https://download.pytorch.org/whl/cu128 \
    && pip install lightning torch_geometric \
    && pip install pyg_lib torch_scatter torch_sparse torch_cluster torch_spline_conv -f https://data.pyg.org/whl/torch-2.7.0+cu128.html \
    && pip install meeko==0.1.dev3"

COPY . /workspace

COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

CMD ["bash", "docker/run_inference.sh"]
