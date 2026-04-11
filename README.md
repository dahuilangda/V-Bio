# V-Bio 药物发现平台

V-Bio 是一个面向药物设计与药物发现流程的计算平台，提供从任务提交、能力路由、集群调度到结果回传的一体化能力。仓库同时包含前端工作台与后端调度执行系统，支持在多台计算服务器上按能力组合部署（如 `boltz2`、`alphafold3`、`protenix`、`pocketxmol`、`lead_opt`）。

## 快速入口

- 首次部署（推荐）：[`docs/deployment/quick-start.md`](docs/deployment/quick-start.md)
- 微服务部署：[`docs/deployment/microservice-decoupling.md`](docs/deployment/microservice-decoupling.md)
- 单机全量安装（微服务版）：[`docs/deployment/single-node-all-apps.md`](docs/deployment/single-node-all-apps.md)
  单机代码更新后可直接执行：`bash deploy/scripts/rebuild_single_node_all_apps.sh`
- 单机快速试跑（10条命令）：[`docs/deployment/single-node-10-commands.md`](docs/deployment/single-node-10-commands.md)
- 能力安装细节：[`docs/deployment/capability-installation.md`](docs/deployment/capability-installation.md)
- Docker/systemd 模板：[`docs/deployment/docker-compose-systemd.md`](docs/deployment/docker-compose-systemd.md)

## 仓库结构

```text
backend/                 后端 API、调度、Celery worker、运行时
capabilities/            各能力实现（boltz2score / affinity / lead_optimization / pocketxmol / colabfold_server 等）
frontend/                V-Bio 前端平台（含 management API）
deploy/                  部署文件（Docker、systemd、脚本）
docs/                    部署、能力、接口文档
backend/scripts/         容器启动入口脚本（API/worker/monitor）
```

Docker 部署入口统一在 `deploy/docker/`，文件名统一使用 `DOCKER_*`。

## 部署原则

1. 计算节点只按各自 `DOCKER_STACK_WORKER_*.env` 声明的能力接任务。
2. 任务按能力队列路由（`cap.*`），不使用旧通用队列。
3. 中央节点与所有 worker 使用同一 `BOLTZ_API_TOKEN`。
4. 前端 `frontend` 只需要对接中央 API，无需感知计算节点细节。

## 最小上线流程

1. 部署中央节点（API + Redis + Monitor）。
2. 部署 GPU 节点并声明本机已安装能力。
3. 部署 CPU 节点（`lead_opt`）并配置 MMP 数据库。
4. 启动 `frontend`，指向中央 API。

完整可复制命令见：[`docs/deployment/quick-start.md`](docs/deployment/quick-start.md)

## 前端运行前置（Python venv）

`frontend` 的 management API 依赖 Python 环境。`frontend/run.sh` 默认会在以下路径查找虚拟环境：

- `/data/V-Bio/venv`
- `frontend/venv`

建议在仓库根目录创建：

```bash
cd /data/V-Bio
python3 -m venv venv
source ./venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

完成后即可使用：

```bash
bash frontend/run.sh start
```

## 一键模板位置

- Docker 模板目录：[`deploy/docker/README.md`](deploy/docker/README.md)
- 安装脚本目录：`deploy/scripts/`
- 单机全量服务一键重建：[`deploy/scripts/rebuild_single_node_all_apps.sh`](deploy/scripts/rebuild_single_node_all_apps.sh)

## 验证接口

上线后先检查：

```bash
curl -H "X-API-Token: <TOKEN>" http://<CENTRAL_HOST>:5000/workers/capabilities
curl -H "X-API-Token: <TOKEN>" http://<CENTRAL_HOST>:5000/workers/cluster_status
```

## 补充文档

- 能力文档目录：[`docs/capabilities/README.md`](docs/capabilities/README.md)
- worker 能力接口：[`docs/apis/worker_capability_api.md`](docs/apis/worker_capability_api.md)
- 后端工程结构：[`docs/architecture/backend_structure.md`](docs/architecture/backend_structure.md)
