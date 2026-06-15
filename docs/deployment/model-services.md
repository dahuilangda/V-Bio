# 模型服务安装

所有模型服务配置都写入对应 stack env 文件，不使用临时 `export`：

- GPU worker：`deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env`
- CPU worker：`deploy/docker/DOCKER_STACK_WORKER_CPU.env`
- 中央服务：`deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env`

先复制模板：

```bash
cd /data/V-Bio
cp deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env.example deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
cp deploy/docker/DOCKER_STACK_WORKER_CPU.env.example deploy/docker/DOCKER_STACK_WORKER_CPU.env
```

## 服务清单

| 服务 | 准备内容 | 关键变量 |
| --- | --- | --- |
| `boltz2` / `boltz2score` / `affinity` | 构建 `vbio-boltz2-runtime`；准备 `/data/boltz_cache`，包含 `boltz2_conf.ckpt`, `boltz2_aff.ckpt`, `ccd.pkl`, `mols.tar` | `BOLTZ2_DOCKER_IMAGE`, `BOLTZ2_HOST_CACHE_DIR` |
| `alphafold3` | 拉取 AF3 镜像；准备模型目录和数据库目录 | `ALPHAFOLD3_DOCKER_IMAGE`, `ALPHAFOLD3_MODEL_DIR`, `ALPHAFOLD3_DATABASE_DIR`, `ALPHAFOLD3_ROOT_HOST` |
| `protenix` | 构建 `vbio-protenix-v2-runtime:2.0.0`；准备源码、权重和 common cache | `PROTENIX_DOCKER_IMAGE`, `PROTENIX_SOURCE_DIR`, `PROTENIX_MODEL_DIR`, `PROTENIX_COMMON_CACHE_DIR` |
| `pocketxmol` | 构建 `pocketxmol:cu128`；准备 checkpoint | `POCKETXMOL_DOCKER_IMAGE`, `POCKETXMOL_ROOT_DIR` |
| ColabFold MSA | 启动独立 MSA 服务 | `MSA_SERVER_URL`, `COLABFOLD_JOBS_DIR` |
| `lead_opt` | 启动 MMP PostgreSQL；导入目标 schema | `LEAD_OPT_MMP_DB_URL`, `LEAD_OPT_MMP_DB_SCHEMA` |

## 常用命令

```bash
docker build -f deploy/docker/DOCKER_BOLTZ2_RUNTIME.Dockerfile -t vbio-boltz2-runtime .
docker build -f deploy/docker/DOCKER_PROTENIX_V2_RUNTIME.Dockerfile -t vbio-protenix-v2-runtime:2.0.0 .

cd /data/V-Bio/deploy/docker
docker compose -f DOCKER_CAP_POCKETXMOL.compose.yml build pocketxmol
docker compose -f DOCKER_CAP_COLABFOLD_SERVER.compose.yml --env-file DOCKER_CAP_COLABFOLD_SERVER.env up -d --build
docker compose -f DOCKER_CAP_MMP_POSTGRES.compose.yml --env-file DOCKER_CAP_MMP_POSTGRES.env up -d --build
```

## GPU worker 示例

```env
GPU_WORKER_CAPABILITIES=boltz2,alphafold3,protenix,pocketxmol
MSA_SERVER_URL=http://<msa-host>:8080

BOLTZ2_DOCKER_IMAGE=vbio-boltz2-runtime
BOLTZ2_HOST_CACHE_DIR=/data/boltz_cache

ALPHAFOLD3_DOCKER_IMAGE=jurgjn/alphafold3:v3.0.2
ALPHAFOLD3_MODEL_DIR=/data/alphafold3/models
ALPHAFOLD3_DATABASE_DIR=/data/alphafold3/databases
ALPHAFOLD3_ROOT_HOST=/data/alphafold3

PROTENIX_DOCKER_IMAGE=vbio-protenix-v2-runtime:2.0.0
PROTENIX_SOURCE_DIR=/data/protenix/source-v2
PROTENIX_SOURCE_DIR_HOST=/data/protenix
PROTENIX_MODEL_DIR=/data/protenix/model
PROTENIX_MODEL_NAME=protenix-v2
PROTENIX_COMMON_CACHE_DIR=/data/protenix/common_cache

POCKETXMOL_DOCKER_IMAGE=pocketxmol:cu128
POCKETXMOL_ROOT_DIR=./capabilities/pocketxmol
```

## CPU worker 示例

```env
CPU_WORKER_CAPABILITIES=lead_opt
LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@<HOST_IP>:54330/leadopt_mmp
LEAD_OPT_MMP_DB_SCHEMA=chembl_cyp3a4_herg
```

导入 MMP schema 时使用 CPU worker 镜像执行 lifecycle CLI：

```bash
docker run --rm --network host   -v /data/V-Bio:/data/V-Bio   -w /data/V-Bio   vbio-worker-cpu-cpu-worker:latest   python -m capabilities.lead_optimization.mmp_lifecycle verify-schema   --postgres_url "postgresql://leadopt:leadopt@<HOST_IP>:54330/leadopt_mmp"   --postgres_schema chembl_cyp3a4_herg
```

## 启动 worker

```bash
docker compose -f deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.compose.yml   --env-file deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env   --profile boltz2 --profile alphafold3 --profile protenix --profile pocketxmol   up -d --build

docker compose -f deploy/docker/DOCKER_STACK_WORKER_CPU.compose.yml   --env-file deploy/docker/DOCKER_STACK_WORKER_CPU.env up -d --build
```
