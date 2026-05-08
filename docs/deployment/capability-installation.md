# 模型服务安装与 Docker 配置

本文档给出各计算任务的最小安装方法，覆盖：`boltz2`、`alphafold3`、`protenix`、`pocketxmol`、`colabfold_server`、`lead_opt(MMP)`。

统一原则：
- 配置写入对应 stack 的 env 文件（`deploy/docker/DOCKER_STACK_*.env`），不要使用临时 `export`。
- 计算节点只声明本机已配置的任务类型。
- 调度按任务类型匹配：没有可用计算节点时直接返回 `503`。
- Docker 安装入口统一放在 `deploy/docker/DOCKER_*`。
- `boltz2` / `boltz2score` / `alphafold3` / `protenix` 统一要求可访问 `MSA_SERVER_URL`。

开始前先准备 GPU 计算节点 env：

```bash
cd /data/V-Bio
cp deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env.example \
  deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
```

并确认 MSA 服务地址（可独立部署在另一台服务器）：

```env
MSA_SERVER_URL=http://<msa-host>:8080
MSA_SERVER_MODE=colabfold
MSA_SERVER_TIMEOUT_SECONDS=1800
COLABFOLD_JOBS_DIR=/data/colabfold/jobs
```

单项配置模板：
- `deploy/docker/DOCKER_CAP_ALPHAFOLD3.env.example`
- `deploy/docker/DOCKER_CAP_PROTENIX.env.example`
- `deploy/docker/DOCKER_CAP_POCKETXMOL.env.example`
- `deploy/docker/DOCKER_CAP_COLABFOLD_SERVER.env.example`
- `deploy/docker/DOCKER_CAP_MMP_POSTGRES.env.example`

## 1. Boltz2（Docker）

Boltz2 统一通过 Docker 子进程执行，不再依赖宿主机 Python 的 Boltz2 运行时。

### 1.1 镜像准备

推荐直接使用仓库内统一 Dockerfile（同时支持 `boltz2` 与 `boltz2score`）：

```bash
cd /data/V-Bio
docker build -f deploy/docker/DOCKER_BOLTZ2_RUNTIME.Dockerfile -t vbio-boltz2-runtime .
```

### 1.2 准备 Boltz 缓存目录（权重与组件文件）

Boltz2 容器运行时会读取 `BOLTZ_CACHE`（默认映射到 `/root/.boltz`）。
建议在宿主机固定目录准备以下关键文件：
- `boltz2_conf.ckpt`
- `boltz2_aff.ckpt`
- `ccd.pkl`
- `mols.tar`

补充：
- `af3.bin.zst` 属于 Boltz1 数据（仅在兼容 Boltz1 时需要）。
- `*.old` 备份文件不会被运行时识别为有效权重。

按官方方式下载：

```bash
mkdir -p /data/boltz_cache
python3 -m pip install -U "huggingface_hub[cli]"

huggingface-cli download boltz-community/boltz-2 \
  --include "*.ckpt" --include "*.json" --include "ccd.pkl" --include "mols.tar" \
  --local-dir /data/boltz_cache

# 可选：如果需要兼容 Boltz1
huggingface-cli download boltz-community/boltz-1 \
  --include "*.ckpt" --include "*.json" --include "ccd.pkl" --include "mols.tar" \
  --local-dir /data/boltz_cache
```

### 1.3 stack env 最小项

```env
GPU_WORKER_CAPABILITIES=boltz2
BOLTZ2_DOCKER_IMAGE=vbio-boltz2-runtime
BOLTZ2_DOCKER_EXTRA_ARGS=
# 必填：宿主机缓存目录，容器会挂载到 BOLTZ_CACHE
BOLTZ2_HOST_CACHE_DIR=/data/boltz_cache
BOLTZ2_CONTAINER_CACHE_DIR=/root/.boltz
```

说明：
- `BOLTZ2_DOCKER_IMAGE` 会被 `boltz2` 与 `boltz2score` 任务共用。
- `BOLTZ2_HOST_CACHE_DIR` 缺失关键文件时，任务会在运行阶段失败。

## 2. AlphaFold3（Docker）

`alphafold3` 通过 Docker 镜像运行。当前使用 `jurgjn/alphafold3:v3.0.2`。

### 2.1 准备镜像与数据目录

- 镜像：`ALPHAFOLD3_DOCKER_IMAGE=jurgjn/alphafold3:v3.0.2`
- 模型目录：`ALPHAFOLD3_MODEL_DIR`（必须是你已下载完成的数据目录）
- 数据库目录：`ALPHAFOLD3_DATABASE_DIR`（必须是你已下载完成的数据目录）
- 宿主机挂载目录：`ALPHAFOLD3_ROOT_HOST`（compose 中用于把宿主机目录挂到 worker）
- 平台不会自动下载 AF3 模型/数据库。

按实际机器执行：

```bash
cd /data/V-Bio
docker pull jurgjn/alphafold3:v3.0.2
docker image inspect jurgjn/alphafold3:v3.0.2 >/dev/null

mkdir -p /data/alphafold3/models /data/alphafold3/databases

sed -i "s|^ALPHAFOLD3_DOCKER_IMAGE=.*|ALPHAFOLD3_DOCKER_IMAGE=jurgjn/alphafold3:v3.0.2|" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s|^ALPHAFOLD3_MODEL_DIR=.*|ALPHAFOLD3_MODEL_DIR=/data/alphafold3/models|" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s|^ALPHAFOLD3_DATABASE_DIR=.*|ALPHAFOLD3_DATABASE_DIR=/data/alphafold3/databases|" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s|^ALPHAFOLD3_ROOT_HOST=.*|ALPHAFOLD3_ROOT_HOST=/data/alphafold3|" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env

# 与后端校验逻辑一致：确认以下 4 个数据库文件存在
ls -lh /data/alphafold3/databases/uniref90_2022_05.fa
ls -lh /data/alphafold3/databases/uniprot_all_2021_04.fa
ls -lh /data/alphafold3/databases/mgy_clusters_2022_05.fa
ls -lh /data/alphafold3/databases/bfd-first_non_consensus_sequences.fasta
```

### 2.2 stack env 示例

```env
GPU_WORKER_CAPABILITIES=alphafold3
ALPHAFOLD3_DOCKER_IMAGE=jurgjn/alphafold3:v3.0.2
ALPHAFOLD3_MODEL_DIR=/data/alphafold3/models
ALPHAFOLD3_DATABASE_DIR=/data/alphafold3/databases
ALPHAFOLD3_DOCKER_EXTRA_ARGS=
ALPHAFOLD3_ROOT_HOST=/data/alphafold3
```

## 3. Protenix v2（Docker）

Protenix v2 部署需要以下宿主机资源：
- 本地运行镜像 `vbio-protenix-v2-runtime:2.0.0`
- 源码目录（包含 `runner/inference.py`）
- 权重目录（包含 `${PROTENIX_MODEL_NAME}.pt`）
- common 数据目录（`PROTENIX_COMMON_CACHE_DIR`）
- 宿主机挂载目录：`PROTENIX_SOURCE_DIR_HOST`

说明：`drailab/protenix:2.0.0` 不包含 `protenix-v2.pt`，并且缺少首次编译 CUDA 扩展所需的 `ninja`。本仓库提供 `DOCKER_PROTENIX_V2_RUNTIME.Dockerfile`，基于该镜像补齐 `ninja-build`。

### 3.1 拉取镜像、准备资源并写入 stack env

```bash
cd /data/V-Bio
docker pull drailab/protenix:2.0.0
docker build -f deploy/docker/DOCKER_PROTENIX_V2_RUNTIME.Dockerfile \
  -t vbio-protenix-v2-runtime:2.0.0 .
docker image inspect vbio-protenix-v2-runtime:2.0.0 >/dev/null

# 1) 准备源码
mkdir -p /data/protenix
git clone --branch v2.0.0 --depth 1 https://github.com/bytedance/Protenix.git /data/protenix/source-v2

# 2) 准备权重与 common 目录
mkdir -p /data/protenix/model /data/protenix/common_cache
curl -L \
  https://huggingface.co/TMF001/pxdesign-weights/resolve/main/checkpoint/protenix-v2.pt \
  -o /data/protenix/model/protenix-v2.pt
sha256sum /data/protenix/model/protenix-v2.pt

# 3) 写入 stack env
cd /data/V-Bio
sed -i "s|^PROTENIX_DOCKER_IMAGE=.*|PROTENIX_DOCKER_IMAGE=vbio-protenix-v2-runtime:2.0.0|" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s|^PROTENIX_SOURCE_DIR_HOST=.*|PROTENIX_SOURCE_DIR_HOST=/data/protenix|" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s|^PROTENIX_SOURCE_DIR=.*|PROTENIX_SOURCE_DIR=/data/protenix/source-v2|" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s|^PROTENIX_MODEL_DIR=.*|PROTENIX_MODEL_DIR=/data/protenix/model|" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s|^PROTENIX_MODEL_NAME=.*|PROTENIX_MODEL_NAME=protenix-v2|" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s|^PROTENIX_COMMON_CACHE_DIR=.*|PROTENIX_COMMON_CACHE_DIR=/data/protenix/common_cache|" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s|^PROTENIX_DOCKER_EXTRA_ARGS=.*|PROTENIX_DOCKER_EXTRA_ARGS=--entrypoint=|" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s|^PROTENIX_PYTHON_BIN=.*|PROTENIX_PYTHON_BIN=/usr/local/micromamba/envs/protenix/bin/python|" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env

# 4) 关键文件检查
ls -lh /data/protenix/source-v2/runner/inference.py
ls -lh /data/protenix/model/protenix-v2.pt
ls -lah /data/protenix/common_cache

# 可选：离线环境建议把 components.cif 预置到 common 目录
# find /data/protenix -name components.cif
# cp /data/protenix/release_data/components.cif /data/protenix/common_cache/
```

stack env 最小项：

```env
GPU_WORKER_CAPABILITIES=protenix
PROTENIX_DOCKER_IMAGE=vbio-protenix-v2-runtime:2.0.0
PROTENIX_MODEL_DIR=/data/protenix/model
PROTENIX_MODEL_NAME=protenix-v2
PROTENIX_SOURCE_DIR=/data/protenix/source-v2
PROTENIX_SOURCE_DIR_HOST=/data/protenix
PROTENIX_CONTAINER_APP_DIR=/app
PROTENIX_CONTAINER_MODEL_DIR=/workspace/model
PROTENIX_CONTAINER_CHECKPOINT_PATH=
PROTENIX_DOCKER_EXTRA_ARGS=--entrypoint=
PROTENIX_PYTHON_BIN=/usr/local/micromamba/envs/protenix/bin/python
PROTENIX_COMMON_CACHE_DIR=/data/protenix/common_cache
```

要求：
- `PROTENIX_SOURCE_DIR` 内必须包含 `runner/inference.py`
- `PROTENIX_MODEL_DIR` 内必须包含 `protenix-v2.pt`

## 4. PocketXMol（Docker）

`pocketxmol` 的 Docker 安装入口统一放在 `deploy/docker/DOCKER_CAP_POCKETXMOL.*`。

### 4.1 构建镜像

```bash
cd /data/V-Bio/deploy/docker
docker compose -f DOCKER_CAP_POCKETXMOL.compose.yml build pocketxmol
docker image inspect pocketxmol:cu128 >/dev/null
```

### 4.2 准备权重

PocketXMol 需要模型权重。按官方 README 下载 `model_weights.tar.gz` 后解压到 `weights/`：

```bash
cd /data/V-Bio/capabilities/pocketxmol
mkdir -p weights
# 把 model_weights.tar.gz 放到当前目录后执行
tar -zxvf model_weights.tar.gz -C .

# 验证关键 checkpoint
ls -lh data/trained_models/pxm/checkpoints/pocketxmol.ckpt
```

### 4.3 stack env 示例

```env
GPU_WORKER_CAPABILITIES=pocketxmol
POCKETXMOL_DOCKER_IMAGE=pocketxmol:cu128
POCKETXMOL_ROOT_DIR=./capabilities/pocketxmol
POCKETXMOL_CONFIG_MODEL=configs/sample/pxm.yml
POCKETXMOL_DEVICE=cuda:0
POCKETXMOL_BATCH_SIZE=50
```

## 5. ColabFold MSA Server（Docker Compose）

ColabFold 是独立 MSA 服务，不参与 worker capability 调度；后端通过 `MSA_SERVER_URL` 调用它。

### 5.1 启动服务

```bash
cd /data/V-Bio/deploy/docker
cp DOCKER_CAP_COLABFOLD_SERVER.env.example DOCKER_CAP_COLABFOLD_SERVER.env
# 按需修改数据库目录、任务目录、config 路径
docker compose -f DOCKER_CAP_COLABFOLD_SERVER.compose.yml \
  --env-file DOCKER_CAP_COLABFOLD_SERVER.env up -d --build
```

### 5.2 中心节点和计算节点 env 对接

```env
MSA_SERVER_URL=http://<colabfold-host>:8080
MSA_SERVER_MODE=colabfold
MSA_SERVER_TIMEOUT_SECONDS=1800
COLABFOLD_JOBS_DIR=/data/colabfold/jobs
```

要求：
- `COLABFOLD_JOBS_DIR` 必须与 ColabFold compose 中 `COLABFOLD_JOBS_DIR` 一致。
- `COLABFOLD_DB_DIR` 需要提前准备足够磁盘空间（数据库体积较大）。

## 6. Lead Opt / MMP（CPU）

`lead_opt` 是 CPU 功能，通常配合 PostgreSQL（可用 Docker）部署。

### 6.1 PostgreSQL（Docker）

```bash
cd /data/V-Bio/deploy/docker
cp DOCKER_CAP_MMP_POSTGRES.env.example DOCKER_CAP_MMP_POSTGRES.env
# 按需修改端口/账号/数据目录
docker compose -f DOCKER_CAP_MMP_POSTGRES.compose.yml --env-file DOCKER_CAP_MMP_POSTGRES.env up -d --build
```

### 6.2 stack env 示例

```env
CPU_WORKER_CAPABILITIES=lead_opt
CPU_MAX_CONCURRENT_TASKS=0
LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@<HOST_IP>:54330/leadopt_mmp
LEAD_OPT_MMP_DB_SCHEMA=chembl_cyp3a4_herg
```

## 7. 多功能 worker 示例

```env
GPU_WORKER_CAPABILITIES=boltz2,alphafold3,protenix,pocketxmol
MAX_CONCURRENT_TASKS=-1
```
