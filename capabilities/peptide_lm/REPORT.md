# PeptideLM 设计系统说明

PeptideLM 是 V-Bio 唯一的多肽设计算法（两段式大模型设计，替换了历史遗传
算法）。设计与基准验证的完整过程记录见 `docs/archive/REPORT_history.md`；
本文档只描述当前系统的结构与事实。

## 架构

**Tier 1 — 预训练先验**（`peplm/models/llama_prior.py`，权重
`models/prior.pt`，附 `models/MANIFEST.json`）
- Llama 式解码器：RoPE / SwiGLU / RMSNorm + 性质回归辅助头
  （溶解度 / 可合成性 / liability，multi-task 预训练）
- 条件化：三性质标签 + 长度桶 token + 结构模态 token `<lin>/<cyc>/<bicy>`
- 50% FIM(PSM) 训练（span infilling 从预训练开始，驱动 Tier2 编辑算子）
- 语料：UniRef90 切段 12M + PDB 挖掘 binder 2257×6 + PeptideGPT 性质集
  10.4k（实验标签）+ NCAA 增强 10%；val loss 2.4132

**Tier 2 — 靶点闭环（纯深度学习，无遗传算法）**
- 生成：序列来自 Tier1 先验的 RL 副本（agent = 先验可训练副本，先验冻结
  作 KL 锚）；de novo 条件采样 + FIM span 编辑 + 点突变
- 学习：GRPO（组相对优势、KL 锚定、截断重要性采样、条件轨迹 prompt-mask）
- 约束：**解码期约束计划**（`peplm/loop/constraints.py`）——固定残基/
  双环 Cys 锚点/NCAA 严格用户池/长度上下界均为解码保证，无事后修补级联
- Oracle：本地 Boltz-2 与 Protenix 双后端；指标 ipTM / pair ipTM /
  几何 ipSAE / min_ipae / per-residue pLDDT；跨后端自洽（top-k 复折叠，
  PAE 矩阵一致性，无 RMSD/无多采样）作为奖励项
- 评分：生产复合分（ipSAE 优先界面 0.58 + binder 0.22 + pair ipTM 0.12
  + 可开发性 0.08）+ min_ipae + 学习型性质头（PeptideGPT 标签训练，
  均值+0.5 地板）+ 自洽项

## 自由度（全部可选，缺省即按靶点自由优化）

| 控制项 | 缺省 | 指定方式 |
|---|---|---|
| 长度 | 自适应 10-30 | `--peptide_len 17`（固定）/ `12 25`（区间）|
| 非天然氨基酸 | 纯天然（无池则解码禁用 NCAA）| `--ncaa_pool AIB CIT`（严格用户池）+ `--user_residue`（自定义 SMILES）|
| 固定残基 | 全序列自由 | `--fixed_residue 5:F`（repeatable，生产 sequence mask 字母同义）|
| 双环 Cys | first+中点+last 自动 | `--cys_positions 7` / 固定 Cys 自动成锚 |
| 模态 | linear | `--design_mode cyclic\|bicyclic` |
| 排名口径 | composite（ipSAE 优先）| `--best_metric ipSAE` |

## 后端边界

| 后端 | linear | cyclic | bicyclic | NCAA（严格用户池）|
|---|---|---|---|---|
| boltz | ✅ | ✅ | ✅（SEZ/29N 键约束）| ✅ |
| protenix | ✅ | ✅（显式 N-C 键）| ✅ | ✅ |
| alphafold3 | ✅ | ❌（仅直链，后端侧拦截报错）| ❌ | ✅（自定义 CCD mmCIF）|

## 生产接入

`backend/runtime/run_single_prediction.py` 的肽设计路径只调用 PeptideLM
提案引擎（`peplm.integrate.backend_proposer`）：初始化为失败即任务失败
（无回退）；每代候选由策略生成（NCAA 种类/位置由策略决定），每代结束后
GRPO 在线学习。前端 `WorkflowRuntimeSettingsSection` 提供设计模式与
自适应长度开关；AI 模型设备由 `VBIO_PEPTIDELM_DEVICE` 控制（默认 cpu）。

## 验证

- 单元/集成测试 `tests/`（CPU，19 项）：表示层、性质 oracle、FIM、约束
  解码保证、一致性数学、真实产物 PAE 提取、后端 proposer 全选项
- 基准与 CD73 实战结果见 `docs/archive/REPORT_history.md`