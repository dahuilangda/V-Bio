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
| `protenix` | 构建 `vbio-protenix-v2-runtime:2.0.0`；准备源码、权重、common cache 和模块缓存目录 | `PROTENIX_DOCKER_IMAGE`, `PROTENIX_SOURCE_DIR`, `PROTENIX_MODEL_DIR`, `PROTENIX_COMMON_CACHE_DIR`, `PROTENIX_MODULE_CACHE_DIR` |
| `nesso` | Virtual Screening 专用：构建固定 Nesso commit 的 `vbio-nesso-runtime:1.0.0`；准备持久化 Hugging Face/CCD cache | `NESSO_DOCKER_IMAGE`, `NESSO_HOST_CACHE_DIR`, `NESSO_MODEL_REVISION` |
| ColabFold MSA | 启动独立 MSA 服务 | `MSA_SERVER_URL`, `COLABFOLD_JOBS_DIR` |

## 模型加载缓存（冷启动加速）

三个推理引擎共用同一个"整模块 pickle 缓存"模式：模型**构造**（随机初始化后立即被 checkpoint 覆盖）每次任务白付几十秒，
缓存把已构造好的模块序列化到宿主机目录，后续任务秒级加载。key 由配置 + checkpoint 身份 + torch 版本决定，任何加载失败自动回退标准路径，
缓存文件可随时删除。

| 引擎 | 缓存目录（宿主机） | 实现 |
| --- | --- | --- |
| boltz2score（亲和力/dock） | `<BOLTZ_CACHE>/module_cache/` | `capabilities/boltz2score/core/model_cache.py`（`BOLTZ2SCORE_DISABLE_MODULE_CACHE=1` 关闭） |
| boltz2 完整预测（boltz_wrapper） | `<BOLTZ_CACHE>/boltz_wrapper_cache/module_cache/` | `backend/runtime/boltz_wrapper.py` → 复用上面的 helper（环境变量 `BOLTZ_WRAPPER_CACHE_DIR` 可重定向） |
| protenix2dock | `/data/protenix/module_cache`（容器内 `/cache/module_cache`，可写挂载） | vendored `runner/inference.py` 的 `init_model()`（env `PROTENIX_MODULE_CACHE_DIR`，由 `backend/worker/docker_cmd.py::protenix_runtime_mounts` 注入） |

实测（CDK2，RTX 4090，冷 → 热）：

- boltz2score：模型加载 25–50s → ~0s（启动地板 ~12s 为 imports）
- boltz2 完整预测：`load_from_checkpoint` ~31s → ~1s（总 51.2s → 24.0s）
- protenix2dock：模型构造 ~83s → ~3s（总 125.7s → 37.9s score / 44.3s dock）

## 常用命令

```bash
docker build -f deploy/docker/DOCKER_BOLTZ2_RUNTIME.Dockerfile -t vbio-boltz2-runtime .
docker build -f deploy/docker/DOCKER_PROTENIX_V2_RUNTIME.Dockerfile -t vbio-protenix-v2-runtime:2.0.0 .
docker build \
  --build-arg HTTP_PROXY=http://<proxy-host>:<proxy-port> \
  --build-arg HTTPS_PROXY=http://<proxy-host>:<proxy-port> \
  -f deploy/docker/DOCKER_NESSO_RUNTIME.Dockerfile \
  -t vbio-nesso-runtime:1.0.0 .

cd /data/V-Bio/deploy/docker
docker compose -f DOCKER_CAP_COLABFOLD_SERVER.compose.yml --env-file DOCKER_CAP_COLABFOLD_SERVER.env up -d --build
```

## GPU worker 示例

```env
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

NESSO_DOCKER_IMAGE=vbio-nesso-runtime:1.0.0
NESSO_HOST_CACHE_DIR=/data/nesso_cache
NESSO_CONTAINER_CACHE_DIR=/workspace/nesso-cache
NESSO_MODEL_REVISION=v1.0.0
NESSO_NO_KERNELS=true
NESSO_RECYCLING_STEPS=5
NESSO_NUM_WORKERS=2
NESSO_PRECISION=bf16-mixed
# 模型首次下载时，把 HTTP_PROXY/HTTPS_PROXY 同时写入该 env 文件

```

## CPU worker 示例

```env
```

```bash
```

## 启动 worker

```bash

docker compose -f deploy/docker/DOCKER_STACK_WORKER_CPU.compose.yml   --env-file deploy/docker/DOCKER_STACK_WORKER_CPU.env up -d --build
```
