# Boltz2

- 任务类型：`boltz2`
- 接口：结构预测（`/predict`，`backend=boltz`）
- 运行节点：GPU 计算节点

## 环境变量

```env
GPU_WORKER_CAPABILITIES=boltz2
BOLTZ2_DOCKER_IMAGE=vbio-boltz2-runtime
BOLTZ2_DOCKER_EXTRA_ARGS=
BOLTZ2_HOST_CACHE_DIR=/data/boltz_cache
BOLTZ2_CONTAINER_CACHE_DIR=/root/.boltz
```

同一台 GPU 机器也可以同时接收多个任务类型：
```env
GPU_WORKER_CAPABILITIES=boltz2,alphafold3
```

## 路由规则

调度到 `cap.boltz2.high` 或 `cap.boltz2.default`。
没有可用计算节点时返回 `503`，不会进入默认队列。

## 安装说明

详见 [docs/deployment/capability-installation.md](../deployment/capability-installation.md) 的 `Boltz2` 小节。

最低缓存文件要求（位于 `BOLTZ2_HOST_CACHE_DIR`）：
- `boltz2_conf.ckpt`
- `boltz2_aff.ckpt`
- `ccd.pkl`
- `mols.tar`

示例下载命令：

```bash
python3 -m pip install -U "huggingface_hub[cli]"
mkdir -p /data/boltz_cache
huggingface-cli download boltz-community/boltz-2 \
  --include "*.ckpt" --include "*.json" --include "ccd.pkl" --include "mols.tar" \
  --local-dir /data/boltz_cache
```
