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

## mol1dock（peptide 模式）

```bash
python3.12 mol1_predict.py --input-json task.json \
    --output-dir $OUT --work-dir $WORK \
    --mode peptide --peptide-chain B --samples 3 --seeds 101,102 \
    --pocket-res 'A:57,A:189' --pocket-max-dist 6.0 \
    --linker-ccd-dir /workspace/linker_ccd
```

- 多 seed/多样本，summary 字段与 protenix2dock 对齐（confidences/best/
  best_by_interface/n_geometrically_clean，附 interface_min_dist/
  interface_clash_pairs/n_interface_hbonds/pocket 报告）
- pocket 为采集后门槛与评分信号（Mol-1 无推理期 pocket 引导）——与平台
  blind 路线协议一致；口袋残基须用 Mol-1 输出 cif 的编号（每实体 1..L 重
  编号，与输入 yaml 编号可能错位，unmatched 会显式列出）
- 平台自定义连接子（SEZ/29N/BS3）：cif 不在 Mol-1 官方 components.cif 中，
  引擎自含解析（atom/bond 表 + ideal 坐标）转 SDF，bond 原子名经
  AtomMapNum 重映射为序号——Mol-1 对 FILE_ 配体会丢弃原原子名

### 实测边界（KLK2 充分测试：12 环肽候选 x 60 样本；双环 + SEZ）

| 维度 | Mol-1（盲折叠） | protenix2dock（staged + TFG 钉住） |
|---|---|---|
| 环肽 ranking 均值 | **0.783**（闭合子集 0.825） | 0.731 |
| 头尾键闭合率 | **20%**（12/60；小环 10aa 高，14aa 低） | 100%（TFG 硬约束） |
| Rama 离群（Top8000） | 闭合子集 **0.0%** | 7.1% |
| pocket 模式 | 5/5 pass（S1 口袋 2.7 Å，编号匹配） | 采集后过滤同构 |
| 双环 SEZ | 桥键以原子重叠满足（退化），ranking ~0.39 | ranking 0.915 |

**结论**：
1. Mol-1 闭环子集的构象质量（Rama 0%、ranking 0.825）优于 protenix2dock，
   但盲折叠的键闭合率仅 20%——**生产用法：多样本（>=10）+ 头尾闭合过滤
   （<=2.5 A），只取闭合子集**。
2. 双环仍以 protenix2dock 为主引擎；Mol-1 定位为环肽候选的快速共折叠
   参考与交叉验证引擎。
