# PeptideLM 架构

## 0. 设计依据（研究结论）

### 0.1 HALO（`capabilities/halo`）— 工程蓝本

HALO 是小分子闭环 lead optimization 的成熟实现，其核心结构：

```
GPT-2 prior（SAFE 片段词表, 25M, 多视图 FIM 式训练）
   │ sample / unified_edit（编辑半径 = 可见环境范围）
   ▼
filter（价键有效性 + 性质窗口 + PAINS + 新颖性/去重记忆）
   ▼
surrogate 门控（bagged ensemble, μ+κσ UCB 主动学习，省 oracle 预算）
   ▼
Boltz2Score oracle（多 GPU, pose+affinity+ipSAE+pLDDT, ~8 s/分子）
   ▼
reward（几何组合：pose 证据门控亲和力 + QED/SAS/cLogP 窗 + 相似度带 + 新颖性,
        batch z-norm 混合防饱和）
   ▼
GRPO（组相对优势按 (编辑上下文, 奖励来源) 分组; KL 二次上界锚定先验;
      TIS 截断保护低概率好样本; 条件轨迹 prompt-mask）
   ▼
（可选）人工偏好 → Bradley-Terry 偏好模型 + DPO
```

关键工程结论（REPORT.md 实证）：多视图拆分训练 > 单视图放大；2 epochs 最佳；
词表设计（digit-isolated + 生成期 FSM 约束）决定有效性；surrogate 门控 +
风险厌恶 surrogate 打分防止未验证分子占便宜；pose 证据门控亲和力防 reward
hacking。CDK2 基准 best pIC50 8.88（已知配体 8.25）。

### 0.2 V-Bio 历史多肽设计（已被 PeptideLM 替换的基线）

生产端（run_single_prediction.py）：
- GA：策略 exploit(0.48)/diversify(0.24)/explore(0.18)/crossover(0.10)，
  pLDDT 加权选位 + BLOSUM 式保守替换；cyclic/bicyclic 模式带 CCD 约束。
- **NCAA = 随机覆盖**：GA 只优化天然碱基序列，`_sample_peptide_modifications`（已删除）
  在原实现中在合格位置随机撒 [min,max] 个 NCAA（placement 规则: PCA 仅 N 端等）。
- 打分：每个候选都跑 Boltz（无 surrogate 门控），composite =
  0.58×界面 + 0.22×binder + 0.12×pair_ipTM + 0.08×可开发性。
- 选择：NSGA-II（4 目标 + 0.92 相似度多样性过滤）。

弱点：无学习先验（随机初始化+局部突变）、NCAA 不可学习、oracle 全额花费、
无跨轮策略梯度。

### 0.3 SOTA 对齐（2024-2026）

- **Peptide-GPT** (arXiv:2410.19222)：GPT 蛋白 LM + 性质标签条件
  （溶血/非污损/溶解），生成后用 PeptideBERT/HAPPENN 分类器过滤 → 我们的
  Tier1 属性标签配方。
- **PepINVENT** (arXiv:2409.14040, Chem Sci 2025) / **PepEVOLVE**
  (arXiv:2511.16912)：REINVENT4 RL 框架扩展到含 NCAA 的多肽，单体级表示
  （HELM/CHUCKLES）+ RL 编辑 → 我们的 Tier2 GRPO + NCAA 单 token 表示。
- **ProtGPT2** (Nat Commun 2022)：残基级 25-token 词表含非标准残基 →
  残基级 token 化可行性的直接证据。
- ** chopped-protein 预训练** (arXiv:2211.06428)：天然肽数据库太小，
  切段蛋白做 LM 预训练是标准解 → 我们的 UniRef 切段语料。
- **Boltz-2 作为 oracle**：社区共识是当排序/奖励信号用（相对值），不当绝对
  ΔG；肽-蛋白复合物无亲和力头（V-Bio affinity eligibility 明确拒绝非
  NONPOLYMER）→ 我们用 ipTM/ipSAE/pLDDT 界面证据组合，与 V-Bio 生产打分
  同源，基准公平。

## 1. 表示层

### 1.1 残基单体词表（`peplm/vocab.py`）

- 20 个天然氨基酸：`A C D E F G H I K L M N P Q R S T V W Y`（单字符 token）
- 18 个 NCAA（V-Bio preset 全集去掉糖基残基）：`[AIB] [NLE] [NVA] [ORN]
  [CIT] [HSE] [HCY] [MSE] [SEC] [HYP] [PCA] [SEP] [TPO] [PTR] [CSO] [MLY]
  [DAL] [BALA]`（括号式单 token）
- 结构 token：`<lin>` / `<cyc>`（序列首，头尾环化）
- 条件 token（Tier1 前缀）：`<dev_hi> <dev_md> <dev_lo>`（溶解+可合成+
  liability 复合分箱）
- 编辑 token：`<cont>`（编辑续写锚点）、`<mask>`（span 编辑）
- 特殊：`<pad> <bos> <eos>`

序列字符串格式：`AC[AIB]GK[CIT]W`（`parse_seq` ↔ token list ↔
(base_sequence, modifications) 三向转换）。canonical 表示（NCAA 写回碱基
字母）与 V-Bio Boltz YAML 的 `modifications: [{position, ccd, baseResidue}]`
一一对应，SMILES 走 `custom_ccd_molecules`（与生产端协议一致）。

### 1.2 NCAA 元数据（`peplm/residues.py`）

每条：CCD 码、SMILES（V-Bio preset 同源）、碱基氨基酸、placement 规则
（PCA→n_term 等）、SPPS 目录可得性、侧链 pKa 修正（CIT/ORN/HSE/PCA 参与净
电荷计算）、残基质量（RDKit 从 SMILES 程序化计算，避免手抄错误）。

## 2. 性质 oracle（`peplm/props/`）— CPU、微秒级

三类分数（0-1，越高越好），同时服务：语料标签、奖励项、过滤器。

### 2.1 溶解度（`solubility.py`）
Kyte-Doolittle 亲水/疏水权重（物理常数，非拟合）+ 净电荷（Henderson-
Hasselbalch，pH 7.4，含 NCAA 侧链 pKa）+ 芳香占比 + 长度项的组合带。
高电荷-低疏水 → 溶；高疏水占比惩罚；带形映射到 0-1。

### 2.2 可合成性（`synthesizability.py`）
SPPS 规则引擎：长度 ≤50；词汇表内全部 NCAA 均为商品化 Fmoc 单体（表内
标注）；聚集/难偶联惩罚（连续 ≥4 疏水、连续 β 折叠倾向残基）；PCA 只在
N 端（placement）；D-残基/β-残基软上限（不影响骨架但增加成本）。

### 2.3 成药性 liability（`liability.py`）
V-Bio `_peptide_sequence_liability_penalty` 扩展：疏水/带电/PG 占比、同聚
物长串、重复三联体，另加脱酰胺（NG/NS/NT）、异构化（DG）、氧化（Met/Trp
长串）、N 端 Q/E 环化倾向。

统一入口 `compute_props(tokens) -> dict`：三类分 + net_charge、mw、
hydrophobic_ratio、ncaa_count、pi_approx。

## 3. Tier 1 — 属性条件先验

### 3.1 语料（`peplm/data/build_corpus.py`）
- UniRef90（本地 18 GB，~1.4 亿序列）：每蛋白按长度分层采样窗口 8-45 aa，
  计算 dev 标签，按 (hi:md:lo ≈ 1:0.6:0.25) 降采样 → ~12M 段。
- PDB binder 肽（RCSB 可达）：经典肽-蛋白复合物（MDM2-p53/Keap1-Nrf2/
  BCL 家族 BH3 等）提取 5-30 aa 结合肽，加权 ×6（真实 binder 分布）。
- 行格式：`<dev_hi> ACDK[AIB]G`（UniRef 段无 NCAA；NCAA token 的分布由
  Tier2 的 GRPO 从奖励中学习——预训练语料天然没有 NCAA，先验对 NCAA 位置
  无偏，策略可塑性最大）。
- 10% 段随机丢标签（CFG 无条件头训练，HALO guidance 同款技巧）。

### 3.2 模型与训练（`peplm/models/gpt2.py`, `train.py`）
HALO `GPT2Prior` 同款：HF GPT2Config（d_model 512 × 8 层 × 8 头 ≈ 26M，
max_len 96），bf16 + cosine + warmup 2% + 早停，batch 384。编码 = 正则
切 token（`(\[[A-Z0-9]{2,4}\}|<[^>]+>|[A-Z])`）。

### 3.3 评估（`scripts/train_tier1.py --eval`）
MOSES 式：validity（可解析+过滤通过率）、unique@k、内部多样性、标签条件
分离度（hi 标签生成样本的 dev 分 vs lo 标签）、性质分布 vs 语料。

## 4. Tier 2 — 靶点闭环

### 4.1 Oracle（`peplm/oracle/peptide_boltz.py`）
- 候选 → Boltz YAML：目标蛋白链（模板 PDB 或序列）+ 肽链
  （sequence=碱基序列, modifications=NCAA 覆盖, custom_ccd_molecules=涉及
  NCAA 的 SMILES）+ `cyclic: true`（环肽）。
- 执行：本地 venv 的 `boltz predict`，候选按 GPU 分片
  并行（一 GPU 一进程摊销模型加载），batch 内失败二分隔离（HALO 同款）。
- 解析：`confidence_*_model_0.json` → iptm、pair_chains_iptm[目标][肽]、
  complex_plddt、per-residue plddt（binder 链均值）；ipSAE 用
  `capabilities/boltz2score` 的 `metrics.ligand_ipsae`（结构与 PAE npz
  后处理，与生产管线同源）。
- 成本：4090 上肽-蛋白复合物（~1200 token）每候选 ~30-60 s（recycling 3、
  diffusion_samples 3，两臂同配置保证公平）。

### 4.2 奖励（`peplm/score/reward.py`）
- 界面证据：`iptm_term`（sigmoid 带，中心 0.62）× `ipsae_term` ×
  `plddt_term` 门控（HALO pose-gating 的肽版：错 pose 的高置信不得分）。
- 性质带：sol / syn / liability（Tier1 oracle 复用）。
- 约束带：ncaa_count ∈ [min,max]（带形，越界硬过滤）、长度带、种子序列
  相似度带（0.25-0.9，太像种子无新意、太不像失去先导关系）。
- 组合：加权几何平均 + batch z-norm 混合（HALO combine_batch 同款）。

### 4.3 Surrogate 门控（`peplm/score/surrogate.py`）
特征：残基组成 + 性质 oracle 输出 + NCAA 统计 + 长度 + 种子相似度。
模型：bagged GradientBoosting 回归 iptm-复合分；UCB = μ + κσ 选入真实
oracle 的候选；未打分行用风险厌恶分（μ − κσ）参与奖励（HALO 防作弊设计）。

### 4.4 GRPO（`peplm/generate/grpo.py`）
HALO `GRPOUpdater` 逐条适配：文本=残基 token 串；组 =（提案上下文，奖励
来源）——edit/mut 按 parent 分组，agent 按同 prompt 同标签分组；条件轨迹
（种子前缀 + `<cont>`）prompt-mask 使 RL 直接改进编辑算子本身；KL 锚定
Tier1 先验；TIS 正优势截断。

### 4.5 引擎（`peplm/loop/engine.py`）
每轮：propose（agent/edit/mut）→ filter（硬窗口 + placement + NCAA 数目 +
去重记忆）→ surrogate UCB 选 oracle 子集 → Boltz 打分 → reward → GRPO →
elite 池更新 → 检查点。结构引导编辑：elite 的低 pLDDT 残基位置用 `<mask>`
span 替换后让模型补全（HALO per-atom pLDDT 编辑图的残基级对应物）。

## 5. 基准协议（`peplm/bench/`）

靶点（PDB 共晶，序列从结构提取，无手抄）：MDM2-p53 (1YCR)、Keap1-Nrf2
(2FLU)、BCL-xL-Bak (1BXL)。两臂同 oracle 配置、同候选预算、同轮数：
- PeptideLM：Tier1 先验 + 闭环。
- GA baseline：逐行为复刻 V-Bio 生产 GA（策略权重/保守替换/随机 NCAA
  覆盖/NSGA-II）。
报告：best/mean top-5 复合分、ipTM 分布、唯一序列数、NCAA 利用率、oracle
调用数、性质分布、每轮收敛曲线。

## 6. 与生产的当前关系

backend 肽设计循环的提案步骤由 PeptideLM 独占（历史上该步骤为遗传算法，
已整体移除）：候选生成/约束/学习全部在 `peplm`（先验 + GRPO 闭环 +
解码期约束计划）内完成；Boltz/Protenix/AF3 子任务调度、打分后处理与
进度上报复用现有管线。前端选项：Design Mode / Peptide Length（可省略，
自适应）/ Residue Pool（NCAA 严格用户池）/ Sequence Mask（固定残基）/
双环 Cys 位置（auto/manual）。
