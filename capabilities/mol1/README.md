# Mol-1 结构预测引擎

Mol-1（Molence Bio，Apache-2.0）的 V-Bio 引擎接入：AF3 同类的开源共折叠
模型，官方基准覆盖蛋白-肽复合物（MolBench-Peptide-26）、抗体-抗原
（FoldBench-AB）、蛋白-配体（PoseBusters）。对环肽设计的价值：**原生支持
环肽头尾酰胺键与二硫键的 polymer-polymer 共价键**（文档列明的例外情形），
且输入格式与平台现有 AF3 风格 adapter 几乎同构。

## 组成

- `Mol-1-source/` 上游源码快照（精简：去掉 assets/examples/tests）
- `Dockerfile` 镜像（cuda12 extra；runtime 基础镜像无 nvcc，故以
  `LAYERNORM_TYPE=torch` 运行 PyTorch LayerNorm，与 boltz2score 的
  `--no_kernels` 同理，24GB 卡更稳）
- `mol1_predict.py` 引擎封装：平台 yaml（boltz 风格键）或 Mol-1 JSON →
  推理 → cif + 置信度汇总

## 输入转换

| 平台 yaml | Mol-1 |
|---|---|
| `protein: {id, sequence, modifications:[{ccd, position}]}` | `proteinChain` + `modifications:[{ptmType: CCD_X, ptmPosition}]` |
| `protein: {cyclic: true}` | 自动补头尾 `N(1)-C(L)` 共价键 |
| `ligand: {ccd: ATP}` / `{smiles: ...}` | `ligand: {ligand: CCD_ATP}` / SMILES 原样 |
| `constraints.bond: {atom1: [chain,pos,atom], ...}` | `covalent_bonds`（chain id → entity 序号） |

## MSA

受体链优先挂平台共享 MSA 缓存（`/data/msa_cache`，按序列 md5 的 a3m，
unpaired 语义）；未命中走 Mol-1 内置 colabfold 搜索，服务地址经
`MOL_MSA_SERVER_URL` 指向平台自建 colabfold 服务（默认
`http://host.docker.internal:8080`）。

## 运行

```bash
docker run --rm --entrypoint= --gpus device=0 \
  -v /data/mol1:/cache:ro \
  -v /data/boltz_msa_cache:/data/msa_cache:ro \
  -v $WORK:$WORK -v $OUT:$OUT --shm-size 16g \
  vbio-mol1-runtime:1.0.0 \
  python3.12 /workspace/mol1_predict.py \
    --yaml /inputs/task.yaml --output-dir $OUT \
    --seed 101
```

宿主机数据：`/data/mol1/checkpoint/mol-1-base.pt`（2.3GB）、
`/data/mol1/common/`（参考数据 0.3GB）。

输出 `<output-dir>/summary.json`：每个 seed 的置信度 JSON 与 cif 路径，
`best` 按 ranking_score 取最优。
