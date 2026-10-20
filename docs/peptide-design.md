# 多肽设计（L/D）机制与验收

## 流程

D 肽设计按镜像协议执行（晶体验收 0.29–0.31 Å，见下）：

1. 靶标结构：上传结构直接使用（仅序列时先做单链预测）；
2. **x→−x 镜像成 D-target**（一次性，坐标精确来自上传结构）；
3. 每个候选序列：孤立肽构象预测（携带环/NCAA/双环拓扑），
   刚体放置到用户口袋（确定性旋转×径向偏移的表面搜索）；
4. 固定 D-target inpainting（受体每步钉死 + 口袋锚带 + 共价键 TFG）；
5. 产物整体翻回显示空间（L-target + D-peptide），受体-受体刚体变换
   恢复上传坐标系，手性硬校验。

L 肽设计跳过镜像，其余机制共用。历史路线（先 de novo 预测 L-L 复合物
再镜像转移）已删除：设计肽无共进化信号，de novo 复合物界面不可靠。

设计空间里引擎看到的链：

| 链 | 内容 | 采样行为 |
| --- | --- | --- |
| A | D-target（镜像坐标，L CCD 命名，残基按序列 1..n 重编号） | 每步钉死在输入位姿（真 inpainting） |
| B | L 肽（proteinChain，不是 SDF） | 从放置位姿去噪；口袋硬锚带 + TFG 接触 |
| L | 双环 linker（CCD，如 SEZ/29N/BS3） | 与肽共价成环（SG↔锚原子） |

关键机制（`backend/runtime/run_single_prediction.py` + protenix2dock peptide 模式）：

- **无 Kabsch 拟合**：staged 复合物直接在镜像空间构造（D-target 精确 +
  构象放置）；产物 frame 用受体-受体刚体变换恢复（同一受体的 CA 对应
  是精确变换，非拟合残差）。
- **双环化学**：构象自带闭环 linker；staged 先做 CCD 键长弛豫，扩散中
  键走 TFG x0 软约束 + 精修后 `_dpeptide_linker_bond_report` 硬门（键长超标即拒）。
- **原子对齐按链内序数**（不按晶体编号），源结构缺失的原子用 CCD 参考
  几何重建；未知原子不 pin。
- **手性守卫**每步重建翻转/拉伸的 CB（设计空间蛋白残基为 L）。
- **口袋编号**：前端/API 的作者编号在上传结构上翻译为序列位置；模板
  上传管线保留作者编号原件（`author_pdb`）用于翻译。

## MSA

受体 MSA 由 colabfold server（MMseqs2 API）提供，缓存于
`/data/boltz_msa_cache`；设计肽查 MSA（env/uniref 分层，超时 1800s 硬失败）。
当前 server 部署按 tier 搜索：env=UniRef30+宏基因组，uniref=仅 UniRef30（缓存键带 tier 后缀）；同一复合物用含 MGYP
宏基因组的全库 MSA 打分时 ipTM 高约 0.25（0.70 vs 0.95，paired 块无
影响）——绝对置信度刻度受 MSA 库内容影响，候选间相对排序不受影响。

## 界面指标（链对限定）

界面置信度按用户声明的链对计算，不用全链平均：

- `--interface_chains 'A,B'`：Dtarget↔L肽（默认，linker 排除——它的
  链对 iptm 会拖低全局值）；
- `pair_iptm` = 跨组最弱链对（引擎 chain-pair 矩阵）；
- `ligand_plddt` = 肽/配体链自身 plddt（全局 plddt 被受体稀释）；
- ipSAE 对声明配体链计算。

## 前端 / API 参数

`POST /predict`（`workflow=peptide_design`，`backend=protenix2dock`）的
`peptide_design_options`（前端项目页肽设计面板同名透传）：

| 参数 | 含义 |
| --- | --- |
| `peptideChirality` | `l` / `d`（`d` 走镜像路径） |
| `peptideDesignMode` | `linear` / `cyclic` / `bicyclic` |
| `peptidePocketResidues` | 口袋残基（如 `A:54,A:61,...`；不填为全局） |
| `peptideLengthMin/Max` | 长度窗口 |
| `peptideResiduePool` | 氨基酸池，含 NCAA（`{"kind":"preset","code":"NLE"}`） |
| `peptideNonNaturalMin/Max` | NCAA 数量窗口 |
| `peptideBicyclicLinkerCcd` | 双环 linker（默认 SEZ） |
| `peptidePopulationSize` / `peptideIterations` | 种群 / 代数 |

## 晶体验收基准

9 个 case 的晶体 redock 基准（3LNJ、8F10 的 D 肽镜像空间 redock，1H1Q、
1HSG 小分子 redock 与各自 blind 对照，BICYC 生产 staged 双环），判定
标准：clash==0、受体钉死、覆盖度==1、无原点原子、pose rmsd≤2.0 Å、
pair_iptm≥0.60、ligand_plddt≥65、ipSAE≥0.10、D 肽手性。

| case | 结果 |
| --- | --- |
| 3LNJ / 8F10 D 肽 redock（镜像空间） | PASS：0.31 / 0.29 Å，clash 0，手性 D |
| 1H1Q / 1HSG 小分子 redock | clash/置信度全过；pose 2.78 / 3.13(oracle) Å（局部调度上界在种子） |
| 1H1Q-blind（`--blind` 标准 diffusion） | PASS：1.83 Å（oracle 1.05）恢复晶体口袋 |
| 1HSG-blind | 未恢复（柔性大配体盲对接，置信头不区分位点） |
| 3LNJ/8F10-blind（肽移出 25 Å） | 采样可达真位点（oracle 2.29 Å），排序不区分位点 |
| BICYC（生产 staged 双环，无晶体） | PASS：键 1.97–2.13 Å 全 bonded、clash 0、手性正确 |

回归测试：在仓库根目录运行
`python3 -m pytest backend/tests/ capabilities/protenix2dock/tests/ -q`。

## 模式锚定设计（A 模式，2026-08-30）

同时上传靶标结构与初始肽结构（同一坐标系，pdb/cif，`peptide_structure_file`
表单字段；前端肽设计设置面板"Initial peptide structure (optional)"）时，
D 路线从通用口袋表面搜索切换为参考姿态锚定：参考肽随靶标镜像进设计空间，
每个候选的孤立构象按 CA 迹（残基序号居中窗口）Kabsch 对齐到参考姿态后
精修（`keep_pose` 跳过二次放置）。只上传其一或都不上传时保持原行为；
`peptideInitialSequence`（参考序列）行为不变。

MDM2 验收（参考 = 3LNJ 晶体肽）：设计肽质心距晶体肽 **0.92 Å**（未锚定
时 7.8-8.2）、界面重叠 **13/16**（未锚定 7-10）、手性 D 正确。

## MSA 搜索

colabfold server 以 `mode=env` 提交（启用宏基因组库搜索），客户端合并
`uniref.a3m + bfd.mgnify30.metaeuk30.smag30.a3m`（按对齐序列去重），缓存键
`msa_<md5>`，预测、af3、boltz2score、protenix2dock 共用。同一晶体位姿 score
校准：**0.703 → 0.960**（仅 UniRef vs 合并；手工富库 0.966）。注意：设计肽为
de novo 序列，其界面置信度天然低于共晶姿态（肽侧无共进化信号）。

## 非天然氨基酸（UNAA）

非天然残基**不能直接打进序列框**（序列只接受标准 20 字母），通过两条通道表达：

### 设计肽侧（Residues used for design）

任务设计器 → "Residues used for design"（展开）：

1. **内置非天然库**（点卡片选用）：D-alanine (DAL)、beta-alanine (BALA)、selenocysteine (SEC)、hydroxyproline (HYP)、pyroglutamate (PCA)、磷酸化 SEP/TPO/PTR、N6-methyllysine (MLY)、S-hydroxycysteine (CSO)、糖基化 O-Man/O-GlcNAc/O-Gal/O-Fuc/O-Glc/O-Xyl 系列。
2. **Custom library → Add**：更冷的 UNAA 用 JSME 画 SMILES（必须含氨基酸骨架 N-CA-C(=O)-O；可选手工指定 N/CA/C/O/Oxt 五原子槽位、C 端酰胺化）。保存进项目 residue pool 后出现在 Custom library 区，点选即可。
3. **Non-natural residue count**（At least / At most）约束设计里非天然残基的数量；未选非天然时为 0/0，此时设计不使用任何 UNAA。
4. 汇总徽标 `X natural / Y non-natural selected` 实时反映选择。

提交后链路：选中的自定义残基 → `custom_ccd_molecules` → 后端 `custom_ccd_builder` 生成 CCD（原子命名/骨架拓扑/可选酰胺化）→ 注入引擎 CCD overlay；设计候选以 `modifications=[{position, ccd, baseResidue}]` 表达。

### 参考蛋白侧（目标组件）

序列框同样只收标准字母。目标蛋白上的非天然残基用 **Residue Modifications**（组件编辑器内）：

- ＋Add → 选 CCD（内置/预置库）或 JSME 画结构 → 在序列预览上点击放置位置（支持 N/C 端修饰）。
- 提交后写入 YAML `protein.modifications`；预置 CCD 库自动按引用合并（`_merge_referenced_preset_modification_molecules`）。

> 常见误解：直接在序列框输入非常规字母会被校验拒绝（"only standard amino acids are allowed"）——这是设计行为，UNAA 一律走位置修饰/残基池，而不是序列字母。

## 与原版 Protenix 的关系

推理引擎为 vendored fork(初始导入自上游, vendor 内 MSA/template/ configs 零改动)。
两条路径:

- **盲推理(天然肽, 无固定靶点)**: 不设置任何 `PROTENIX_*` 侧信道 env, 行为与
  上游逐字一致(默认 1 seed × 5 sample × 200 step)。
- **固定靶点精修/双环路线**: 仅两处非原版——① 受体 pin(固定靶点 inpainting,
  用户上传坐标即坐标事实); ② 共价键约束(双环链接器 SG↔锚原子的官方 TFG
  键/角约束与 CCD 键带投影, 维持环拓扑)。采样数与上游对齐(5)。

侧链塌陷/clash 的残余噪声由交付门禁把关(键长带、锚点自碰撞、主链完整性、
界面深度), 不合格样本被淘汰而非发货。对照实验可用 `PROTENIX_DISABLE_TFG=1`
与 `PROTENIX_DISABLE_CHEMISTRY=1` 关闭全部定制约束。
