# 功能：Protenix

- capability 名称：`protenix`
- 典型任务：结构预测（`/predict`，`backend=protenix`）
- worker 类型：GPU

## `.env` 配置

```env
GPU_WORKER_CAPABILITIES=protenix
PROTENIX_DOCKER_IMAGE=ai4s-share-public-cn-beijing.cr.volces.com/release/protenix:1.0.0.4
PROTENIX_SOURCE_DIR_HOST=/data/protenix
PROTENIX_MODEL_DIR=/data/protenix/model
PROTENIX_MODEL_NAME=protenix_base_20250630_v1.0.0
PROTENIX_SOURCE_DIR=/data/protenix/source
PROTENIX_COMMON_CACHE_DIR=/data/protenix/common_cache
PROTENIX_CONTAINER_APP_DIR=/app
PROTENIX_CONTAINER_MODEL_DIR=/workspace/model
# 可选：如果 checkpoint 不是默认路径
# PROTENIX_CONTAINER_CHECKPOINT_PATH=/workspace/model/protenix_base_20250630_v1.0.0.pt
```

## 路由规则

调度到 `cap.protenix.high` 或 `cap.protenix.default`。
无在线 worker 时返回 `503`，不会进入默认队列。

## 最低文件要求

- `${PROTENIX_SOURCE_DIR}/runner/inference.py`
- `${PROTENIX_MODEL_DIR}/${PROTENIX_MODEL_NAME}.pt`
- `${PROTENIX_COMMON_CACHE_DIR}`（需可读写，用于 Protenix common 数据；离线环境建议预置 `components.cif`）

## 安装说明

详见 [docs/deployment/capability-installation.md](../deployment/capability-installation.md) 的 `Protenix（Docker）` 小节（官方镜像实操部署）。

最短安装命令（单机）：

```bash
docker pull ai4s-share-public-cn-beijing.cr.volces.com/release/protenix:1.0.0.4
mkdir -p /data/protenix
git clone https://github.com/bytedance/Protenix.git /data/protenix/source
cd /data/protenix/source
python3 -m protenix.data.download_protenix_data --data part_weights --download_dir /data/protenix
python3 -m protenix.data.download_protenix_data --data part_data --download_dir /data/protenix
```
