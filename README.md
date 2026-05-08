# V-Bio

V-Bio 用于管理结构预测和药物发现计算任务。仓库包含前端页面、中心 API、任务调度服务、CPU/GPU 计算节点，以及 Docker Compose 部署配置。

## 目录结构

```text
backend/                 后端 API、调度服务、计算任务运行时
capabilities/            模型和算法的接入代码
frontend/                前端页面和管理 API
deploy/                  Docker、systemd、部署脚本
docs/                    部署、接口、模型服务和架构文档
backend/scripts/         API、计算节点、monitor 启动脚本
```

Docker 部署入口在 [`deploy/docker/`](deploy/docker/README.md)。文件名统一使用 `DOCKER_*`。

## 当前模型服务

GPU 计算节点环境文件：

```text
deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
```

已配置的镜像和运行参数：

```dotenv
ALPHAFOLD3_DOCKER_IMAGE=jurgjn/alphafold3:v3.0.2

PROTENIX_DOCKER_IMAGE=drailab/protenix:2.0.0
PROTENIX_MODEL_NAME=protenix-v2
PROTENIX_SOURCE_DIR=/data/protenix/source-v2
PROTENIX_MODEL_DIR=/data/protenix/model
PROTENIX_DOCKER_EXTRA_ARGS=--entrypoint=
PROTENIX_PYTHON_BIN=/usr/local/micromamba/envs/protenix/bin/python
```

Protenix-v2 权重文件：

```text
/data/protenix/model/protenix-v2.pt
```

权重 SHA256：

```text
8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599
```

## 文档入口

| 内容 | 文档 |
| --- | --- |
| 首次部署 | [`docs/deployment/quick-start.md`](docs/deployment/quick-start.md) |
| 微服务部署 | [`docs/deployment/microservice-decoupling.md`](docs/deployment/microservice-decoupling.md) |
| 单机全量部署 | [`docs/deployment/single-node-all-apps.md`](docs/deployment/single-node-all-apps.md) |
| 单机 10 条命令试跑 | [`docs/deployment/single-node-10-commands.md`](docs/deployment/single-node-10-commands.md) |
| 模型服务安装 | [`docs/deployment/capability-installation.md`](docs/deployment/capability-installation.md) |
| Docker/systemd 模板 | [`docs/deployment/docker-compose-systemd.md`](docs/deployment/docker-compose-systemd.md) |
| 模型服务文档 | [`docs/capabilities/README.md`](docs/capabilities/README.md) |

单机代码更新后可执行：

```bash
bash deploy/scripts/rebuild_single_node_all_apps.sh
```

## 部署原则

1. 中心节点运行 API、Redis、monitor 和前端。
2. 计算节点只接收本机已配置的任务类型。
3. 任务按队列路由，例如 `cap.alphafold3`、`cap.protenix`。
4. 中心节点与计算节点使用同一个 `BOLTZ_API_TOKEN`。
5. 生产环境固定镜像 tag，不使用 `latest`。

## 基础启动

复制环境变量模板：

```bash
cp deploy/docker/DOCKER_STACK.env.example deploy/docker/DOCKER_STACK.env
cp deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env.example deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
```

启动基础服务：

```bash
docker compose \
  -f deploy/docker/docker-compose.yml \
  --env-file deploy/docker/DOCKER_STACK.env \
  up -d
```

查看基础服务：

```bash
docker compose -f deploy/docker/docker-compose.yml ps
```

## 启动 GPU 计算节点

启动 AlphaFold3：

```bash
docker compose \
  -f deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env \
  --profile alphafold3 \
  up -d gpu-worker-alphafold3
```

启动 Protenix：

```bash
docker compose \
  -f deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env \
  --profile protenix \
  up -d gpu-worker-protenix
```

查看 GPU 计算节点：

```bash
docker compose \
  -f deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env \
  ps
```

## 前端本地运行

`frontend/run.sh` 默认查找以下 Python 虚拟环境：

```text
/data/V-Bio/venv
frontend/venv
```

推荐在仓库根目录创建：

```bash
cd /data/V-Bio
python3 -m venv venv
source ./venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

启动前端和管理 API：

```bash
bash frontend/run.sh start
```

更多前端配置见 [`frontend/README.md`](frontend/README.md)。

## 验证

检查中心 API：

```bash
curl -H "X-API-Token: <TOKEN>" http://<CENTRAL_HOST>:5000/workers/capabilities
curl -H "X-API-Token: <TOKEN>" http://<CENTRAL_HOST>:5000/workers/cluster_status
```

检查本地前端代理：

```bash
curl http://localhost:5173/api/health
curl http://localhost:5173/supabase/projects
```

检查计算节点日志：

```bash
docker logs vbio-gpu-worker-alphafold3 --tail 80
docker logs vbio-gpu-worker-protenix --tail 80
```
