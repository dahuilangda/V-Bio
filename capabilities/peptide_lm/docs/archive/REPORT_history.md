# PeptideLM 基准报告

## v4 架构（现代主干 + 全模态 + 用户可指定一切）

研究依据（不闭门造车，逐条对应文献/工程实现）：
- **Tier1 主干**：GPT-2（2018 年架构，v1-v3）→ **Llama 式解码器**
  （RoPE 旋转位置 Su et al. 2021 / SwiGLU Shazeer 2020 / RMSNorm）——
  现代蛋白/化学 LLM 的标准组件（ProtGPT2/ProGen2 一代之后的主流工程基线；
  每行/每模态的工程参照见 training 脚本注释）。
- **性质回归辅助头**（multi-task LM，ESM/ProtTrans 同款思想）：mean-pool
  隐藏态 → 溶解度/可合成性/liability 三值回归，loss = CE + 0.1·MSE。
- **模态 token `<lin>/<cyc>/<bicy>`**：一个先验直接服务直链/环肽/双环肽；
  训练时 84% 直链 / 8% 环 / 8% 双环（双环行自动施加 first/interior/
  last Cys 布局，与 Tier2 解码约束完全一致）。
- **FIM(PSM) 50%**（Bavarian/ProtFIM/IgLM/PepMLM 配方）——Tier2 的
  "固定上下文重设计"编辑算子从预训练就开始训练。
- **语料分阶段**（领域共识）：UniRef90 切段 12M + PDB 挖掘 binder 2257×6
  + PeptideGPT 性质集 10.4k（实验标签）+ NCAA 增强 10%。

**用户可指定一切（自由优化原则）** —— 全部可选，缺省即按靶点自由优化：

| 控制项 | CLI | 缺省（自由优化） |
|---|---|---|
| 长度 | `--peptide_len 17`（固定）/ `12 25`（区间） | 8-25 自适应（FIM 编辑天然支持插入/删除） |
| 双环 Cys | `--cys_positions 7` / 固定残基 Cys 自动成锚 | first+中点+last 自动 |
| 非天然氨基酸 | `--ncaa_pool AIB CIT ...`（**严格用户池**） | 未指定 = **纯天然**（池外 token 解码级禁用） |
| 固定残基 | `--fixed_residue 5:F` / `9:[AIB]`（repeatable） | 全序列自由 |
| 任意自定义 aa | `--user_residue residues.json`（SMILES 注册+词表扩展+oracle CCD） | 无 |

实现跨度：`peplm/residues.py` 用户注册表、`models/*` 动态词表扩展
（HF resize + 均值初始化 + prior 行复制保 KL 锚定）、`models/gpt2.py`
PlacementMask 严格池 + 池外禁用、`loop/engine.py` 布局/固定/配额修补
（固定位置全算子保护、布局在 NCAA 修补后重施加）、`oracle/*` 后端无关、
`integrate/backend_proposer.py` 消费生产 peptideResiduePool/SequenceMask
（长度可省略）。12/12 CPU 测试覆盖上述语义。

## CD73 双环肽实战（Protenix 后端，用户新布局）

平台项目 3bf7199e：老 GA 双环（boltz）best **0.797**。本轮新增 Protenix
后端、Cys first/interior/last 布局、长度 12-25 自适应、NCAA 0-3：

| 运行 | 后端 | best（生产复合分） | 备注 |
|---|---|---|---|
| GA（生产记录） | boltz | 0.797 | 双环固定布局 |
| PeptideLM seed0 | boltz | **0.8004** | 192 oracle，刷纪录 |
| PeptideLM seed1 | boltz | 0.7906 | 双种子均值 0.796 |
| **PeptideLM** | **protenix** | **0.5246** | protenix 尺度独立（旧项目无 protenix-双环），top 候选 pair_iptm 0.80 / plddt 71.4，长度 12-25 全程自适应 |

Protenix top：`CKKGIFHCPIRTISDYYFKGC`（len 21，ipTM 0.816，pair 0.801，
plddt 71.4）。protenix 的 ipSAE 走 PAE-only 近似（token_pair_pae 无坐标），
数值低于 boltz 的 dist+pae 版——跨后端比较应以同后端为基准。

## v4 评估（Llama 式主干 vs GPT-2）

| 指标 | v2 final (GPT-2) | **v4 final (Llama 式, 2 epochs)** | 说明 |
|---|---|---|---|
| val loss | 2.4141 | **2.4132** | 略优；epoch0 即达 v2 终值 |
| tag 分离度 | 0.253 | 0.253（epoch0 0.263） | mean_dev hi 0.845 / lo 0.592 |
| FIM 中段 token 准确率 | 22.0% | 17-22%（探针方差） | >> 随机 5% |
| 长度条件化 | L15→15-19 | **L15→15-19** | 解码级保证 |
| 唯一性 / 多样性 | 1.0 / 0.93 | **1.0 / 0.93** | — |

结论：在 26M 参数/25M tokens 预算下的主流 LM 损失上，现代主干与优化良好
的 GPT-2 基本打平；结构性收益在别处——RoPE 长度泛化、**单先验服务
直链/环肽/双环三模态**、动态词表支撑任意用户氨基酸、以及与生产
peptideResiduePool/SequenceMask 一一对应的自由优化语义。质量提升的下一
步（已入路线图）是把模态/长度/固定位置做成硬条件化 + 更大规模
（100M+/100M tokens+），并让 Tier2 的 GRPO 在新先验上重跑基准。

## "best" 的口径与 ipSAE 根源修复

**公式解读**：生产复合分 `best = 0.58×interface + 0.22×binder + 0.12×pair_ipTM
+ 0.08×developability`，其中 interface 严格按 ipSAE 优先
（`ipsae_dom → ligand_ipsae_max → pair_ipTM`）——best 的第一大权重项
本来就是 ipSAE。潜在隐患不在权重，而在 **ipSAE 数值的可比性**。

**根源问题（已修）**：Protenix 后端此前算 ipSAE 只用 token 级 PAE（无几何
距离），且因"蛋白质 CA 数（172）≠ PAE token 数（183，含 SEZ linker
配体 token）"的相等性误判走了退化路径。修复：
- 链块→token 组按**尺寸匹配**对齐（155=靶点、17=binder、配体组跳过），
  蛋白 token 为数组前缀，按组最小索引排序拼接 CA 坐标；
- ipSAE 恢复为与 boltz 路径同一公式：CA 距离 ≤10 Å ∩ PAE<12；
- 重算 CD73 Protenix 全部 191 候选，**err 从"全部退化"降为 0**，排名发生
  真实变化（几何口径下 top 变为 `CGYEDVFCYYLSYDRHQSGFSC` 0.4761，
  原 top `CKKGIFHCPIRTISDYYFKGC` 0.4740，二者 ipsae 0.300/0.243）。

**双口径可选**：`--best_metric composite|ipSAE`——composite 维持与生产
可比（默认），ipSAE 用纯界面证据排名；`scored.jsonl` 每行同时记录
`composite` 与 `best` 两个值，rounds 日志输出 `best_ipSAE`。

**诚实提醒**：跨后端比较要看同后端。Protenix 界面的 ipSAE（CA-CA 10Å）
系统性低于 boltz 版（≈0.2-0.3 vs 0.5-0.8），这是两模型接口分布差异而非
bug；报"best"时请注明后端与口径。

## 与 SOTA 评分的对齐核查（2024-26 逐篇核实）与升级

**领域共识**（AlphaProteo 2409.08022 / Latent-X 2507.19375 / PepFlow ICML24 /
PepGLAD / Vilya-1 / RL-PLM 2510.01571 等）：湿实验 SOTA 的主信号是**共折叠
结构置信度家族**（ipTM / pTM / min interchain pAE / binder pLDDT / complex
RMSD 自洽门控）+ **学习型性质头**（Vilya 的 PAMPA/动力学溶解度头）+
实验结合为最终仲裁；物理打分（Rosetta ΔG<0）只作生成论文的成功定义，无
SOTA 用作主排名。本轮据此核查我们的评分并提出两个升级：

| 维度 | 原实现 | SOTA 对齐升级（已落地） |
|---|---|---|
| 界面置信度 | ipSAE（自命名，≈界面 PAE 均值） | 新增 **`min_ipae` / `mean_ipae`**（target×binder 全对 PAE 最小值/均值，Å，Latent-X/AlphaProteo 同名术语），boltz+protenix 两端均计算，作为奖励项（min_ipae ≤6Å→1）与 scored.jsonl 记录字段 |
| 可开发性 | 纯规则（sol/syn/liab oracle） | 新增**学习型性质头**（PeptideGPT 实验标签：可溶/非污损/非溶血，GradientBoosting 组合特征，留出 AUC 0.99+，`runs/pepgpt_props.pkl`）；奖励项 = 三头均值 + 0.5 地板（shaping 而非硬门） |
| 排名口径 | composite | `--best_metric composite|ipSAE`，scored.jsonl 双指标 |

**诚实局限**：学习型头的留出 AUC 高但**带池偏置**——PeptideGPT 三个单类集
互相可分，头在区分"来源池"而非普适性质（与 Peptide-GPT 同款问题）；故只作
低权重 shaping 项（0.6），不做硬门。ipSAE 为自命名指标，SOTA 对应物是
interchain pAE/ipAE；我们用 min_ipae 对齐其词表与语义。两个 SOTA 旗舰的
ipTM 权重未收敛（AlphaProteo 0.8 / Latent-X 0.2），我们维持生产 0.58+0.12
组合以便与 V-Bio 历史可比，并保留 ipSAE 纯口径选项。

## 升级 1+3 工程化落地（无多采样、无 RMSD；GPU 空出后执行真实计算）

**升级 3 — 约束感知解码**（`peplm/loop/constraints.py` + 两个采样器 + 引擎）：
- `ConstraintPlan` 在**解码期**强制：固定残基（硬强制，FIM 内按"发射相对
  坐标"映射、prefix 内由组装时应用用户指定）、双环 Cys 锚点（已知长度时
  0/中点/末端全解码期）、NCAA **严格用户池**（池外 token 解码禁用）、
  NCAA 下限的**确定性解码期强制**（取代随机配额修补）、长度上下界为解码
  保证。
- 原"多残基事后修补级联"（锚点被配额修补破坏→重施加 的 bug 类）→
  过滤器改**纯验证**（违反即拒）；自适应长度下仅剩 2 残基有界 post-edit
  （末端/中部锚）。
- `ncaa_decode_bias`（软偏置）缓解实测的 NCAA 回避。

**升级 1 — 跨后端自洽奖励**（`peplm/oracle/interchain_pae.py` +
`peplm/loop/consistency_guard.py`）：
- **无 RMSD 无多采样**：每轮 top-k（默认 8）用**独立权重**的另一后端
  （boltz↔protenix，CLI `--consistency_backend`）复折叠一次；
  `self_consistency = corr(PAE_A, PAE_B) × 1/(1+e^{(|Δmin_ipae|−2)})`——
  对齐无关、对靶点柔性免疫；成为 reward 项（权重 0.7）与 scored.jsonl 记录。
- 两个 oracle 记录 PAE 提取路径（`record_dir` / `protenix_pred_root`）。
- 单模型自信循环（mdm2 平台期的"高自信低成绩"）由此获得交叉验证信号。

**验证（全部 CPU，GPU 空出后跑真实）**：17/17 测试（含约束解码、FIM 相对
坐标、PAE 一致性数学、从真实 boltz/protenix 产物提取、guard 合并）；
静态 import 清理；backend 语法/导入复验通过。CLI：
`--consistency_backend protenix --consistency_topk 8 --ncaa_decode_bias 0.5`。

（以下为历史版本存档）

## v2 升级（学习 ProteinMPNN/LigandMPNN 与 2024-26 多肽设计 SOTA 后）

研究结论（要点）：领域共识是**分阶段语料**（通用蛋白 LM → 生物活性肽数据库
25-60k → 结合对任务适配 ~10k）；**FIM/span-editing 必须从预训练开始**
（Bavarian 2207.14255 / ProtFIM / IgLM / PepMLM 的 binder-span 掩码 =
ProteinMPNN "固定上下文重设计" 的 LM 形态）。v1 的两大缺口（编辑只能重生成
尾部、复合 dev 标签无信息量）据此修复：

| 升级 | v1 | v2 | 证据 |
|---|---|---|---|
| 训练格式 | 纯 LM | +50% FIM(PSM) 行 | FIM 中段 token 准确率 22%（随机 5%） |
| 条件化 | 单 dev 标签（分离度 0.01） | 三性质标签+长度桶 | 分离度 **0.253**（26×）；提示 L15 → 采样 15-19 |
| binder 语料 | 4 条手工肽 | **2257 条** PDB 挖掘（分辨率分片搜索+兄弟实体 GraphQL）×6 | PepBench/PepMLM 量级 |
| 性质数据 | 无 | PeptideGPT 三集 10.4k（实验标签） | BroadAMP-GPT 配方 |
| Tier2 编辑 | 尾部重生成 | **FIM span 编辑**（双翼上下文，掩低 pLDDT 段） | MPNN 式固定上下文重设计 |

v2 先验 val 2.414（v1 2.669），唯一性 1.0，多样性 0.93。

## 基准结果（生产复合分，best / top5-mean）

denovo 设定（无先导信息）：

| target | LM v1 | **LM v2** | GA | v2-GA best |
|---|---|---|---|---|
| mdm2 | 0.755 / 0.734 | 0.788 / 0.772 | 0.850 / 0.824 | -0.062 |
| keap1 | 0.842 / 0.797 | 0.847 / 0.828 | 0.876 / 0.849 | -0.029 |
| bclxl | 0.743 / 0.598 | **0.882 / 0.858** | 0.792 / 0.730 | **+0.090** |

lead-opt 设定（两臂均从共晶种子肽出发）：

| target | LM v1 | **LM v2** | GA | v2-GA best |
|---|---|---|---|---|
| mdm2 | 0.828 / 0.798 | 0.861 / 0.830 | 0.897 / 0.855 | -0.036 |
| keap1 | 0.879 / 0.868 | **0.881 / 0.878** | 0.876 / 0.849 | **+0.005** |
| bclxl | 0.917 / 0.881 | **0.922 / 0.897** | 0.792 / 0.730 | **+0.130** |

- v2 > v1 于**全部 6 个设定×靶点组合**（+0.003 至 +0.139）。
- 对 GA：lead-opt 2/3 胜（bclxl +0.130 大胜、keap1 胜），denovo 1/3 胜
  （bclxl +0.090），mdm2 两设定仍负（p53 走廊窄，GA 单点突变恰是最优算子）。

## CD73 双环肽实战验证（用户的真实项目）

项目 3bf7199e（V-Bio 生产平台）：CD73 催化域 155 aa，双环模式
（Cys 3/8/17 + SEZ linker），12 代 × 16 = 192 oracle 调用，生产复合分。
老 GA 的双环最好成绩 **0.797**（linear/cyclic 分别 0.911/0.898——双环是
最难的设定，即用户所述"无法发现好的双环肽"）。

PeptideLM v2 同预算双环运行（scripts/run_cd73_bicyclic.sh）：

| run | best | top 候选 |
|---|---|---|
| GA（生产记录） | 0.797 | — |
| **PeptideLM seed0** | **0.8004** | KSCTLPACPLVAEVITC（ipTM 0.943, ipSAE 0.786, pLDDT 76.8） |
| PeptideLM seed1 | 0.7906 | AICDFGICAGPHTVREC（ipTM 0.947, ipSAE 0.743） |

双种子结论（诚实）：0.8004 / 0.7906 vs GA 0.797——**同预算持平略胜，
seed0 刷新项目纪录**（+0.003，在 oracle 噪声 ±0.01 量级内）。这与基准结论
一致：无先导、短预算、强约束（3×Cys+linker 固定布局）下，GA 的直接存活
选择仍是强基线；PeptideLM 的优势场景（lead-opt、更长预算）在此未完全
释放。前 5 密集 0.78-0.80、全部 ipTM ≥ 0.94。NCAA 使用率低（≤11% 候选）
——双环布局约束下策略保守；将 ncaa 下限设 ≥1 可强制探索（建议后续）。
要显著超越 0.80：给 PeptideLM 一个先导（如老项目 linear 0.911 胜肽改双环）
+ 24 轮，是明确的下一步。

（以下为 v1 报告存档）

两臂同 oracle（本地 Boltz-2：boltz2 模型、recycling 3 / sampling 200 /
diffusion 3、多 GPU 分片）、同预算（8 轮 × 16 候选 = 128 次 oracle 调用）、
同 NCAA 配额（1-3 个/肽，17 个 preset 全池）。报告指标 = V-Bio 生产复合分
（0.58×界面 + 0.22×binder + 0.12×pair_ipTM + 0.08×可开发性），与生产系统直接可比。

靶点（PDB 共晶，序列从结构文件提取）：

| target | PDB | 受体 | 种子肽 |
|---|---|---|---|
| mdm2 | 1YCR | MDM2 N 端域 (109 aa) | p53 TAD: SQETFSDLWKLLPEN |
| keap1 | 2FLU | Keap1 Kelch (308 aa) | Nrf2: AFFAQLQLDEETGEFL |
| bclxl | 1BXL | BCL-xL (221 aa) | Bak BH3: GQVGRQLAIIGDDINR |

两种设定：
- **denovo**：两臂均无先导信息（生产默认场景）。
- **leadopt**：两臂均拿到共晶种子肽（LM：锚定编辑 + 首轮打分；GA：
  initial_sequence，对齐生产 peptideInitialSequence 行为）。

## 全部结果（生产复合分，best / top5-mean）

| target | 设定 | PeptideLM | GA（生产复刻） | Δ best | Δ top5 |
|---|---|---|---|---|---|
| mdm2 | denovo | 0.7548 / 0.7342 | 0.8497 / 0.8235 | -0.095 | -0.089 |
| keap1 | denovo | 0.8418 / 0.7965 | 0.8758 / 0.8490 | -0.034 | -0.052 |
| bclxl | denovo | 0.7430 / 0.5984 | 0.7923 / 0.7301 | -0.049 | -0.132 |
| mdm2 | leadopt | 0.8279 / 0.7981 | 0.8973 / 0.8553 | -0.069 | -0.057 |
| keap1 | leadopt | **0.8786 / 0.8682** | 0.8758 / 0.8490 | **+0.003** | **+0.019** |
| bclxl | leadopt | **0.9172 / 0.8806** | 0.7923 / 0.7301 | **+0.125** | **+0.151** |

GA 双种子方差（denovo best）：mdm2 ±0.007、keap1 ±0.000、bclxl ±0.023。

## lead-opt 的胜因（bclxl 案例，从 oracle 原始输出重建）

优胜序列（复合分 0.928，ipTM 0.982、binder pLDDT 91.4）：

```
G[NVA]VGRQLAIIGDDINR     <- Bak BH3 的 L4 -> norvaline 单点 NCAA 编辑
GQVGRQLAIIGDDINR         <- 种子本身 0.890
GQVGQQLAIIGDDI[MLY]R     <- R15 -> N6-甲基赖氨酸
GQV[HYP]RQLAIIGDDINH     <- L5 -> 羟脯氨酸
GQVGRQ[AIB]AIVGDDINR     <- L7 -> Aib
```

策略学到的是**最小化、化学上合理的靶点导向 NCAA 编辑**——norvaline（Val
同系物 +CH2）、Aib（α,α-二甲基，helix 增强剂）、Hyp、MLy 都是 SPPS 目录
单体，全部满足 placement 规则。这正是 V-Bio "NCAA 随机覆盖" 做不到的：
GA 在两设定下都停在 0.792，从未找到这些编辑。

## 诚实的负结果（denovo 与 mdm2-leadopt）

- denovo 全负：8 轮 × ~40 样本内，GRPO 来不及把通用先验转移到靶点特异
  高分区；GA 的 NSGA-II 直接在报告指标上做存活选择，短预算天然占优。
  keap1 denovo 第 5 轮出现过 0.879 单候选（≥GA），说明高分区可达，瓶颈
  是策略转移速度不是表示/oracle。
- mdm2-leadopt 负：GA 从 p53 种子出发的保守替换走廊极窄（F/W/L 热点
  几乎不可动），GA 单点突变恰好是这种走廊的最优算子；LM 的尾部重生成
  编辑步长偏大。
- Tier1 标签条件分离度弱（<dev_hi> vs <dev_lo> 生成均值仅差 0.01）：
  分位数校准后语料本身的 dev 分布集中，标签信息量有限；CFG 引导已实现
  但当前先验下中性。

## 结论与建议

1. **生产接入（已完成）**：backend 支持 `options.peptideAlgorithm =
   "peptidelm"`（opt-in；失败自动回退 GA）。建议：有先导序列（用户上传
   或已有 hit）时用 peptidelm（lead-opt 场景 2/3 靶点胜，bclxl +0.125）；
   冷启动 denovo 或超短预算时仍用 GA。
2. **缩小 denovo 差距的下一步**（未做，路径明确）：更长的闭环轮数或
   每轮更大 oracle 预算；把 reward 的界面项直接对齐复合分（当前 pose 门控
   更保守）；Tier1 加长度/性质 token 条件化（标签分离度）；HALO 式
   DPO 人工偏好通道（代码结构已留位）。
3. Tier2 是深度学习（GRPO + 学习型 surrogate 门控），无传统 GA 组件；
   GA 仅存在于基准对照与回退路径。

## 复现

```bash
PY=/data/Boltz2Score/.venv/bin/python; cd /data/V-Bio/capabilities/peptide_lm
$PY -m pytest tests/ -q                                    # 9 项 CPU 测试
$PY scripts/summarize_bench.py runs/bench/report.json runs/bench_leadopt/report.json
# 优胜者重建（不依赖引擎记账，直接从 boltz 输出解析）见
# runs/bench_leadopt/bclxl_lm/oracle/**/yaml + confidence_*.json
```

训练资产：runs/prior/prior.pt（25.3M，val 2.669，12M 段语料，
性质标签分位数校准 + 3% NCAA 增强 + 10% 丢标签 CFG 样本）。
