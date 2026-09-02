# HALO — 闭环先导化合物优化

HALO 以生成式模型为核心做小分子先导化合物优化，支持三种模式：

| 模式 | 说明 |
| --- | --- |
| `denovo` | 在口袋内从头生成 |
| `fragment` | 片段替换：保留用户指定片段，改写其余部分 |
| `scaffold_hop` | 骨架跃迁：按比例把编辑投向骨架变换 |

工作方式：SAFE 表示上的 GPT-2 先验提出候选；平台的预测引擎打分
（protenix2dock 默认，可选 boltz2dock、alphafold3，经 V-Bio 任务队列提交）；
GRPO 在闭环中更新策略；surrogate 模型做主动学习门控，为每轮筛选省下
oracle 预算。人可以随时介入偏好反馈，奖励分布随之改变。

## 目录与运行时工件

```
halo/
├── cli.py              # 独立运行入口（pretrain / focus / optimize / run …）
├── config.py           # HaloConfig / LoopConfig
├── vbio_runner.py      # V-Bio 任务系统的运行入口
├── generate/           # 先验、词表、SAFE 任务、RL 更新器
├── loop/               # 闭环引擎、多样性、人类反馈
├── score/              # 奖励、性质、偏好模型、新奇度索引
├── oracle/             # 预测 oracle（平台打分协议）
├── data/               # 配体表、语料加载、ChEMBL 近邻
└── runs/               # 运行时工件（不进 git，见下）
```

`runs/` 存放先验权重与新奇度语料，体积大、不随仓库分发，部署时需自备
（训练产出或从已有部署拷贝）：

| 文件 | 用途 |
| --- | --- |
| `runs/<prior>/prior.pt` | 生成先验权重 |
| `runs/<prior>/agent.pt` | RL 策略初始权重（与 prior 同构） |
| `runs/<prior>/vocab.json` 或 `digit_bpe_tokens.json` | 词表（fragment / atom 切分，或 SAFE digit-BPE） |
| `runs/<prior>/model_meta.json` | 架构元数据 |
| `runs/chembl36_safe.smi` | ChEMBL36 SAFE 语料（新奇度索引） |
| `runs/chembl36_corpus.fp2048.npz` | 语料 Morgan 指纹缓存 |

平台任务默认读 `runs/prior_mv_rag2`，提交 payload 里的 `prior_dir` 可以
指向其他目录。文件缺失时任务直接报错，不做静默回退；`fp2048.npz` 缺失
只影响新奇度奖励项，引擎照常运行。

## 数据准备

先验训练的语料是 ChEMBL SMILES。`pretrain` 默认读
`data/chembl_raw_data.txt`（ChEMBL 官方 TSV，下载地址见
https://www.ebi.ac.uk/chembl/ 的 Downloads 页），也可用 `--chembl` 指向
任意"一行一个 SMILES"的文件；`optimize` / `run` 的近邻检索默认用
`data/chembl_compounds.smi`（两列：SMILES、ChEMBL ID）。

新奇度语料 `runs/chembl36_safe.smi` 是 ChEMBL36 分子的 SAFE 线性化结果，
用 `generate/safe_tasks.safe_encode_robust` 逐分子编码即可重建；
`.fp2048.npz` 是其 Morgan 指纹缓存，`focus` 首次运行或
`data/neighbors.build_corpus_fingerprints` 会自动生成。

## 训练先验

字符/片段词表先验（产物：`vocab.json`、`prior.pt`、`agent.pt`、
`model_meta.json`）：

```bash
python -m halo.cli pretrain \
  --run_dir runs/my_prior \
  --chembl data/chembl_raw_data.txt \
  --model gpt2 --epochs 2
```

`--tokenizer fragment`（默认）先挖高频片段构建 fragment-regex 词表，
`--tokenizer atom` 退回原子级切分；`--model` 可选 `gpt2` 或
`transformer`。预训练结束后自动采样做有效性/唯一性自检。

SAFE digit-BPE 先验（生产所用的 `prior_mv_rag2` 即此形态）的训练原语在
`generate/safe_prior.py`：`train_bpe` 训练 BPE 词表（对应
`digit_bpe_tokens.json`），`train` 执行 T1/T2/T3 多任务预训练（无条件
生成、片段边界 prefix continuation、core-masked 骨架跃迁翻译）。

目标聚焦续训：

```bash
python -m halo.cli focus \
  --reference <lead.sdf 或 SMILES> \
  --base_prior runs/prior_mv_rag2 \
  --run_dir runs/my_prior_focused
```

`focus` 以参考化合物在 ChEMBL 语料中检索近邻、加权后继续训练，得到更
贴合骨架的先验。

## 运行

平台内通过 Lead Optimization 工作区提交
（`POST /api/lead_optimization/halo_optimize`，
`GET /api/lead_optimization/halo_status/<task_id>`），轮次进度、每轮
候选与最终结果（`halo_results.json`、`candidates.csv`、`rounds.jsonl`）
都会进入任务结果归档。

独立运行走同一 oracle 协议：

```bash
python -m halo.cli optimize \
  --protein target.pdb \
  --reference lead.sdf \
  --mode scaffold_hop --scaffold_hop 0.3 \
  --run_dir runs/cdk8 --rounds 6
```

无 GPU 环境加 `--mock-oracle` 可跑通完整闭环（随机打分），用于冒烟测试。

## 测试

```bash
python -m pytest tests/test_smoke.py -q
```
