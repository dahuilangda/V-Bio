# Protenix

- 任务类型：`protenix`
- 接口：结构预测（`/predict`，`backend=protenix`）
- 运行节点：GPU 计算节点

## 环境变量

```env
GPU_WORKER_CAPABILITIES=protenix
PROTENIX_DOCKER_IMAGE=vbio-protenix-v2-runtime:2.0.0
PROTENIX_SOURCE_DIR_HOST=/data/protenix
PROTENIX_MODEL_DIR=/data/protenix/model
PROTENIX_MODEL_NAME=protenix-v2
PROTENIX_SOURCE_DIR=/data/protenix/source-v2
PROTENIX_COMMON_CACHE_DIR=/data/protenix/common_cache
PROTENIX_CONTAINER_APP_DIR=/app
PROTENIX_CONTAINER_MODEL_DIR=/workspace/model
PROTENIX_CONTAINER_CHECKPOINT_PATH=
PROTENIX_DOCKER_EXTRA_ARGS=--entrypoint=
PROTENIX_PYTHON_BIN=/usr/local/micromamba/envs/protenix/bin/python
```

## 路由规则

调度到 `cap.protenix.high` 或 `cap.protenix.default`。
没有可用计算节点时返回 `503`，不会进入默认队列。

## 最低文件要求

- `${PROTENIX_SOURCE_DIR}/runner/inference.py`
- `${PROTENIX_MODEL_DIR}/${PROTENIX_MODEL_NAME}.pt`
- `${PROTENIX_COMMON_CACHE_DIR}`（需可读写，用于 Protenix common 数据；离线环境建议预置 `components.cif`）
- 本地镜像 `vbio-protenix-v2-runtime:2.0.0`

## 安装说明

详见 [docs/deployment/capability-installation.md](../deployment/capability-installation.md) 的 `Protenix v2（Docker）` 小节。

最小安装命令：

```bash
docker pull drailab/protenix:2.0.0
docker build -f deploy/docker/DOCKER_PROTENIX_V2_RUNTIME.Dockerfile \
  -t vbio-protenix-v2-runtime:2.0.0 .

mkdir -p /data/protenix
git clone --branch v2.0.0 --depth 1 https://github.com/bytedance/Protenix.git /data/protenix/source-v2
mkdir -p /data/protenix/model /data/protenix/common_cache
```

`protenix-v2.pt` 下载到：

```text
/data/protenix/model/protenix-v2.pt
```

当前可用下载地址：

```text
https://huggingface.co/TMF001/pxdesign-weights/resolve/main/checkpoint/protenix-v2.pt
```

SHA256：

```text
8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599
```
