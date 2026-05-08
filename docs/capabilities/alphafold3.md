# AlphaFold3

- 任务类型：`alphafold3`
- 接口：结构预测（`/predict`，`backend=alphafold3`）
- 运行节点：GPU 计算节点

## 环境变量

```env
GPU_WORKER_CAPABILITIES=alphafold3
ALPHAFOLD3_DOCKER_IMAGE=jurgjn/alphafold3:v3.0.2
ALPHAFOLD3_ROOT_HOST=/data/alphafold3
ALPHAFOLD3_MODEL_DIR=/data/alphafold3/models
ALPHAFOLD3_DATABASE_DIR=/data/alphafold3/databases
ALPHAFOLD3_DOCKER_EXTRA_ARGS=
```

同一台 GPU 机器也可以同时接收多个任务类型：

```env
GPU_WORKER_CAPABILITIES=alphafold3,protenix,pocketxmol
```

## 路由规则

调度到 `cap.alphafold3.high` 或 `cap.alphafold3.default`。
没有可用计算节点时返回 `503`，不会进入默认队列。

## 安装说明

详见 [docs/deployment/capability-installation.md](../deployment/capability-installation.md) 的 `AlphaFold3（Docker）` 小节。

最小安装命令：

```bash
docker pull jurgjn/alphafold3:v3.0.2
mkdir -p /data/alphafold3/models /data/alphafold3/databases
```

AlphaFold3 官方模型和数据库需要单独下载到上面的目录。运行前确认 `ALPHAFOLD3_DATABASE_DIR` 至少包含：

- `uniref90_2022_05.fa`
- `uniprot_all_2021_04.fa`
- `mgy_clusters_2022_05.fa`
- `bfd-first_non_consensus_sequences.fasta`
