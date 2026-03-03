# 功能：ColabFold MSA Server

- 服务名称：`colabfold_server`（独立 MSA 服务，不是 worker capability）
- 用途：为预测任务提供本地 MSA API（`/ticket/msa`、`/ticket/{id}`、`/result/download/{id}`）
- 运行方式：Docker Compose（统一入口 `deploy/docker/DOCKER_CAP_COLABFOLD_SERVER.compose.yml`）

## `.env` 关联配置

后端 stack env（例如 `deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env`，必要时同步到 worker 的 `DOCKER_STACK_WORKER_*.env`）：

```env
MSA_SERVER_URL=http://<MSA_HOST>:8080
MSA_SERVER_MODE=colabfold
MSA_SERVER_TIMEOUT_SECONDS=1800
COLABFOLD_JOBS_DIR=/data/colabfold/jobs
```

ColabFold Compose 专用 env：

```env
COLABFOLD_DB_DIR=/data/colabfold/databases
COLABFOLD_JOBS_DIR=/data/colabfold/jobs
COLABFOLD_CONFIG_PATH=/data/V-Bio/capabilities/colabfold_server/config.json
COLABFOLD_ENABLE_GPU=0
MMSEQS_DOWNLOAD_URL=https://mmseqs.com/latest/mmseqs-linux-gpu.tar.gz
```

GPU 说明：
- 当前只保留单一 MMseqs 二进制（建议 GPU 包），不再区分 avx2/gpu 双下载。
- 设 `COLABFOLD_ENABLE_GPU=1` 后，服务会在数据库处理阶段启用 MMseqs GPU 参数。
- 需保证容器可访问 GPU（例如 Docker 配置了 `nvidia` runtime 或 compose 服务启用 `gpus: all`）。

## 运行规则

- ColabFold 是独立服务，不进入 Celery 功能队列。
- `COLABFOLD_JOBS_DIR` 需要与 ColabFold 容器挂载目录一致，便于后端复用任务结果文件。

## 安装说明

详见 [docs/deployment/capability-installation.md](../deployment/capability-installation.md) 的 `ColabFold MSA Server（Docker Compose）` 小节。
