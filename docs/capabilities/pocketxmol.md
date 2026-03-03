# 功能：PocketXMol

- capability 名称：`pocketxmol`
- 典型任务：lead-opt 候选提交（`backend=pocketxmol`）
- worker 类型：GPU

## `.env` 配置

```env
GPU_WORKER_CAPABILITIES=pocketxmol
POCKETXMOL_DOCKER_IMAGE=pocketxmol:cu128
POCKETXMOL_ROOT_DIR=./capabilities/pocketxmol
POCKETXMOL_CONFIG_MODEL=configs/sample/pxm.yml
POCKETXMOL_DEVICE=cuda:0
POCKETXMOL_BATCH_SIZE=50
```

## 路由规则

调度到 `cap.pocketxmol.high` 或 `cap.pocketxmol.default`。
无在线 worker 时返回 `503`，不会进入默认队列。

## 最低文件要求

- `deploy/docker/DOCKER_CAP_POCKETXMOL.compose.yml`
- `deploy/docker/DOCKER_CAP_POCKETXMOL.Dockerfile`
- `${POCKETXMOL_ROOT_DIR}/scripts/run_pocketxmol_docker.sh`
- `${POCKETXMOL_ROOT_DIR}/weights/model_weights.tar.gz`（或已解压后可直接加载的模型文件）

## 安装说明

详见 [docs/deployment/capability-installation.md](../deployment/capability-installation.md) 的 `PocketXMol（Docker）` 小节。

补充：
- 先执行 `docker compose -f deploy/docker/DOCKER_CAP_POCKETXMOL.compose.yml build pocketxmol`。
- 运行时镜像可通过 `POCKETXMOL_DOCKER_IMAGE` 指定（默认 `pocketxmol:cu128`）。
- 权重需按 PocketXMol 官方说明下载到 `weights/` 目录（见该能力 README）。
