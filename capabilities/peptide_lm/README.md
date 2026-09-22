# PeptideLM — 两段式大模型多肽设计（Tier 1 属性先验 + Tier 2 靶点闭环）

PeptideLM 用语言模型做 V-Bio 的多肽设计，与
[HALO](../halo/README.md) 的闭环 lead-optimization 架构同源：先验 →
Boltz-2/Protenix oracle → GRPO 策略学习 → surrogate 主动学习门控。表示层
为**残基级单体制语言**（天然氨基酸 + 非天然氨基酸单 token，携带 CCD/SMILES
元数据，对齐 V-Bio 的 Boltz YAML `modifications` 协议）。

## 架构

**Tier 1 — 现代主干先验**（`peplm/models/llama_prior.py`，`--arch modern`）
- Llama 式解码器：RoPE / SwiGLU / RMSNorm
- 辅助性质回归头（溶解度/可合成性/liability，multi-task LM 预训练）
- 三性质条件标签 + 长度桶 token（IgLM/ProGen 式控制标签）
- 50% FIM(PSM) 训练（span infilling 从预训练开始——PepMLM/ProtFIM 配方）
- 模态 token `<lin>/<cyc>/<bicy>`：一个先验服务直链/环肽/双环肽三种设计
- 语料（分阶段，领域共识）：UniRef90 切段 12M + PDB 挖掘 binder 2257 条
  （×6）+ PeptideGPT 性质集 10.4k（实验标签）+ NCAA 增强 10%

**Tier 2 — 学习型闭环**（纯深度学习，无 GA）
- GRPO 策略优化（KL 锚定先验、截断重要性采样、条件轨迹 prompt-mask）
- FIM span 编辑算子（ProteinMPNN "固定上下文重设计" 的 LM 形态：
  掩掉低 pLDDT 段、双翼上下文重填，天然支持插入/删除→**长度自适应**）
- 学习型 surrogate UCB 门控（省 oracle 预算的主动学习）
- 双环肽：3 Cys 锚点（显式位置或 first/interior/terminal 布局）+ SEZ/29N linker 键约束
  （V-Bio 生产协议）；Oracle 双后端：本地 Boltz-2 或 Protenix（docker）

**自由优化原则（所有控制项都可选）**
- **长度**：不指定 → 算法按目标自由探索（默认 8-25 自适应）；指定单值 =
  固定长度
- **半胱氨酸位置**：不指定 → 自动（first_last 布局取中点锚）；指定绝对
  位置 + 用户固定的 Cys 自动成为锚点
- **非天然氨基酸 = 用户指定池**：`--ncaa_pool AIB CIT ...`（从 18 个
  preset 目录或自定义 SMILES 中选择）；**未指定池 = 纯天然设计**（池外
  token 解码级禁用）；池内用哪个、放哪里由 GRPO 学习
- **固定残基**：`--fixed_residue 5:F` / `9:[AIB]`（repeatable，生产
  peptideSequenceMask 语义）；不指定 = 全序列自由优化
- **任意自定义氨基酸**：`--user_residue residues.json`（`[{ccd, smiles,
  base, placement}]`）运行时注册 + 词表扩展 + oracle CCD 缓存

## 快速开始

依赖通过 pip 安装（`pip install -r requirements.txt`，未提供 requirements
时按 `peplm/` 导入错误补齐即可）。以下命令均在 `capabilities/peptide_lm`
目录下执行：

```bash
# 1) CPU 单测
python -m pytest tests/ -q

# 2) Tier1 语料 + 现代架构预训练
#    语料源是 UniRef90 FASTA，从 UniProt FTP 下载后经 --uniref 传入：
#    当前版本 https://ftp.uniprot.org/pub/databases/uniprot/current_release/uniref/uniref90/uniref90.fasta.gz
#    （与生产语料完全对齐用 previous_releases/release-2022_05/uniref/
#    的 uniref2022_05.tar.gz）。
#    产出 runs/data/uniref_train.txt / uniref_val.txt，其中 822MB 的
#    训练切分不进 git，可随时用本命令重建。
python -m peplm.data.build_corpus --n_segments 12000000 --out_dir runs/data
python scripts/train_tier1.py --data runs/data --out models --arch modern \
    --device cuda:0 --epochs 2 --ncaa_aug 0.10

# 3) 闭环设计（直链/环肽/双环肽；boltz 或 protenix 后端；任意用户残基）
#    --run_dir 缺省写入统一运行输出根（VBIO_RUNS_DIR，默认 /data/vbio_runs）。
python scripts/run_closed_loop.py --target mdm2 --gpus 0 1 2 3 \
    --prior models/prior.pt --rounds 8
python scripts/run_closed_loop.py --protein <SEQ> --design_mode bicyclic \
    --cys_positions 7 --bicyclic_layout first_last --linker SEZ \
    --peptide_len 12 25 --ncaa 0 3 --backend protenix --gpus 1 2 3 \
    --user_residue my_residues.json --rounds 12

# 4) 基准：PeptideLM vs V-Bio 生产 GA（同 oracle 同预算）
python scripts/run_benchmark.py --targets mdm2 keap1 bclxl --arm lm --gpus 1 2 3 ...
python scripts/summarize_bench.py /data/vbio_runs/peptide_lm/bench*/report.json
```

## 生产接入（默认引擎）

backend 肽设计路径**只**使用 PeptideLM（`peplm/integrate/backend_proposer.py`）。
提供受体序列时，生产管线由 **PepMLM-650M**（ChatterjeeLab, Nat Biotech 2025，
`models/external/pepmlm650/`）承担。研究管线（`peplm/models/esm3_policy.py`）
提供 ESM3-open-1.4B + LoRA + GRPO 在线强化学习路径（`peplm/loop/esm3_loop.py`，
驱动 `scripts/run_esm3_loop.py`）：d2 逐步 PPO 比率、Dr.GRPO 优势、k3 KL 锚定
adapter-disabled 基座。奖励为轮内相对 z-score（冷启动策略的全部候选都过不了任何
绝对门——常数奖励使组优势恒零，实测确认），训练信号用稠密界面 PAE，ipSAE 作
验收指标；逐残基 pLDDT+界面 PAE 构成 credit 向量，驱动父代弱位重掩码与 GRPO
逐 token 优势权重。父代兄弟 refill 组成独立 GRPO 组（≥2 成员）。oracle 为
protenix2dock 肽模式盲对接（溶剂侧锚盒定位 + 2.6Å 深埋护栏 + 主链立体守卫：
全残基 CA 手性/omega 平面性/芳香环平面性），样本选择按真实结合肽包络
（3LNJ 晶体 D 肽：最小间距 2.52Å、CA-CA 3.73、接触分数 0.81）分档：
立体化学纯净 → 骨架完好 → 无深埋 → 界面 PAE。提案格式为受体+全掩码肽连续拼接
（ESM3 开放权重单链预训练，无 chainbreak）。

- **de novo**：掩码填充采样（探索），NCAA 在合法位随机插入；
- **natural refine**：mask 精英中 oracle 逐残基 pLDDT 最弱的残基再补全
  （定向修复），精英的 NCAA 位点原样保留——模型围绕非天然残基重优化
  天然上下文；
- **NCAA evolve**：迁移/换型一个精英 NCAA（利用）——迁移优先落到 oracle
  低置信位（柔性点正是构象约束单体发挥作用的位点），换型在用户池内
  遵守放置规则。

解码严格尊重前端残基池选择（屏蔽的氨基酸不出现），环肽/直链模式自动
屏蔽游离 Cys；排名用天然 base 序列的 pseudo-PPL（模型词表仅天然氨基酸，
对 NCAA 移动不敏感——因此各流用保留配额而非纯 PPL 竞争）。无受体时走
Tier-1 属性先验（性质标签 + SS 条件化 + FIM 编辑 + NCAA 点移动，每代
GRPO 在线学习）。两条路径失败即上报（无回退无兜底）；打分/调度/进度
上报复用现有管线。详见
[docs/BACKEND_INTEGRATION.md](docs/BACKEND_INTEGRATION.md)。

## 与现有系统的关系

- NCAA 元数据（CCD/SMILES/碱基/位置规则）与 Boltz YAML 构造完全复用
  V-Bio 协议（`backend/runtime/run_single_prediction.py` 的 preset 表），
  生成的候选可直接被现有 /predict 管线消费。
- `peplm/bench/ga_baseline.py` 逐行为复刻 V-Bio 生产 GA（策略权重、保守
  替换表、随机 NCAA 覆盖、NSGA-II 精英），保证基准公平。

