# 功能：AlphaFold3

- capability 名称：`alphafold3`
- 典型任务：结构预测（`/predict`，`backend=alphafold3`）
- worker 类型：GPU

## `.env` 配置

```env
GPU_WORKER_CAPABILITIES=alphafold3
ALPHAFOLD3_DOCKER_IMAGE=cford38/alphafold3
ALPHAFOLD3_ROOT_HOST=/data/alphafold3
ALPHAFOLD3_MODEL_DIR=/data/alphafold3/models
ALPHAFOLD3_DATABASE_DIR=/data/alphafold3/databases
```

或与其他功能组合：
```env
GPU_WORKER_CAPABILITIES=alphafold3,protenix,pocketxmol
```

## 路由规则

调度到 `cap.alphafold3.high` 或 `cap.alphafold3.default`。
无在线 worker 时返回 `503`，不会进入默认队列。

## 安装说明

详见 [docs/deployment/capability-installation.md](../deployment/capability-installation.md) 的 `AlphaFold3（Docker）` 小节。

最短安装命令（单机）：

```bash
docker pull cford38/alphafold3
mkdir -p /data/alphafold3/models /data/alphafold3/databases
# 将 AF3 官方模型与数据库下载到以上目录
```

运行前请确保 `ALPHAFOLD3_DATABASE_DIR` 至少包含：
- `uniref90_2022_05.fa`
- `uniprot_all_2021_04.fa`
- `mgy_clusters_2022_05.fa`
- `bfd-first_non_consensus_sequences.fasta`
