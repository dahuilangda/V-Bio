# `.env` 配置说明

## 最小配置

```env
REDIS_URL=redis://localhost:6379/0
CENTRAL_API_URL=http://localhost:5000
BOLTZ_API_TOKEN=development-api-token
```

## Docker 栈配置边界

- 微服务部署只使用 `deploy/docker/DOCKER_STACK_*.env`。
- 容器配置请统一在对应 stack 的 `.env` 文件中维护，不再依赖仓库根目录 `.env`。

## 调度与并发（推荐默认）

```env
MAX_CONCURRENT_TASKS=-1
CPU_MAX_CONCURRENT_TASKS=0
GPU_WORKER_CAPABILITIES=
CPU_WORKER_CAPABILITIES=
```

含义：
- `MAX_CONCURRENT_TASKS=-1`：GPU worker 默认使用本机全部可用 GPU。
- `CPU_MAX_CONCURRENT_TASKS=0`：CPU worker 默认使用本机全部 CPU 核心。
- `GPU_WORKER_CAPABILITIES=` 留空：GPU worker 默认具备全部 GPU 功能。
- `CPU_WORKER_CAPABILITIES=` 留空：CPU worker 默认具备全部 CPU 功能。
- `GPU_POOL_NAMESPACE=`：可选；为 GPU 池 Redis 键加命名空间（多 capability 微服务并行时建议设置）。

功能代码目录默认在 `capabilities/`（例如 `lead_optimization`、`pocketxmol`）。
ColabFold MSA 服务的 Docker 入口统一在 `deploy/docker/DOCKER_CAP_COLABFOLD_SERVER.*`。

Protenix 官方镜像使用 host-mounted 资源：
- `PROTENIX_DOCKER_IMAGE=ai4s-share-public-cn-beijing.cr.volces.com/release/protenix:1.0.0.4`
- `PROTENIX_SOURCE_DIR_HOST=/data/protenix`
- `PROTENIX_SOURCE_DIR=/data/protenix/source`
- `PROTENIX_MODEL_DIR=/data/protenix/model`
- `PROTENIX_COMMON_CACHE_DIR=/data/protenix/common_cache`

AlphaFold3 常用 host-mounted 目录：
- `ALPHAFOLD3_ROOT_HOST=/data/alphafold3`
- `ALPHAFOLD3_MODEL_DIR=/data/alphafold3/models`
- `ALPHAFOLD3_DATABASE_DIR=/data/alphafold3/databases`

PocketXMol 建议使用独立 Docker 镜像：
- `POCKETXMOL_DOCKER_IMAGE=pocketxmol:cu128`
- `POCKETXMOL_ROOT_DIR=./capabilities/pocketxmol`
- `POCKETXMOL_CONFIG_MODEL=configs/sample/pxm.yml`

## 多功能声明示例

```env
GPU_WORKER_CAPABILITIES=alphafold3,protenix,pocketxmol
CPU_WORKER_CAPABILITIES=lead_opt,peptide_design
BOLTZ2_DOCKER_IMAGE=vbio-boltz2-runtime
BOLTZ2_HOST_CACHE_DIR=/data/boltz_cache
```

一个 worker 可声明多个功能；系统会自动展开为多个 `cap.*` 队列监听。
系统不会再监听任何旧通用队列；是否接任务仅取决于你在对应 stack env 声明的功能。
`BOLTZ2_DOCKER_IMAGE` 会被 `boltz2`、`boltz2score`（以及依赖 Boltz runtime 的 affinity 执行链）共用。
`BOLTZ2_HOST_CACHE_DIR` 建议预置 `boltz2_conf.ckpt`、`boltz2_aff.ckpt`、`ccd.pkl`、`mols.tar`。

## 部署参考

- 首次部署：`docs/deployment/quick-start.md`
- 微服务解耦部署：`docs/deployment/microservice-decoupling.md`
- 单机全量微服务部署：`docs/deployment/single-node-all-apps.md`
- Docker Compose + systemd 模板：`docs/deployment/docker-compose-systemd.md`
- Docker 文件统一入口：`deploy/docker/DOCKER_*`
