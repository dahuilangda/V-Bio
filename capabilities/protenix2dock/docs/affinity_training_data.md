# protenix2dock 原生 affinity head：训练数据研究与策展方案

来源研究（一手资料）：
- Boltz-2 论文全文（bioRxiv 2025.06.14.659707 / PMC12262699），Data 章节 + Table 1
- Nesso-1 技术报告（valencelabs.com, 2026.07, §2.1-2.3）
- 二者为我们 head 的直接参照系（protenix trunk + 独立 affinity 头 = 同族设计）

---

## 一、Boltz-2 的 affinity 训练数据（Table 1，过滤后）

| 来源 | 用途 | Binders | Decoys | 靶点(90%序列聚类) | 化合物 |
| --- | --- | ---: | ---: | ---: | ---: |
| ChEMBL + BindingDB | 优化值(回归) | 1.2M | 0 | 2k | 600k |
| PubChem 小 assay | hit发现(双向) | 10k | 50k | 250 | 20k |
| PubChem HTS | hit发现(二分类) | 200k | 1.8M | 300 | 400k |
| CeMM Fragments | hit发现(二分类) | 25k | 115k | 1.3k | 400 |
| MIDAS 代谢物 | hit发现(二分类) | 2k | 20k | 60 | 400 |
| ChEMBL+BindingDB 合成 decoys | 二分类负例 | 0 | 1.2M | 2k | 600k |

### 四层质量策展（论文原文要点）

1. **只留高质量 assay**：单蛋白靶点；biochemical/functional 类别；排除
   low-confidence/unreliable 标记；回归值只取 Ki/Kd/IC50/AC50/EC50/XC50，
   统一 log10(µM)；"数据不足或亲和力标准差过低的 assay 丢弃"——强迫模型
   学 **intra-assay 差值** 而非 inter-assay 偏移。
2. **抗偏差合成 decoys**：hit-to-lead 的 binder 跨靶点 shuffle 造负例；
   要求 decoy 与该蛋白所有已知 binder 的 Tanimoto < 0.3（控制假阴性）。
   扩大负例覆盖 + 消 HTS 的伪相关。
3. **结构质量过滤**：iptm < 0.75 的靶点剔除（boltz2 自己的共折叠置信度）。
4. **化合物过滤**：PAINS 过滤 + 重原子数 ≤ 50。

HTS 额外规则：assay ≥ 100 化合物、hit rate < 10%（滤噪声筛选）；
HTS 阳性要求在**独立 assay** 有定量测量（Ki/Kd/XC50）佐证。

## 二、Nesso-1 的差异（技术报告 §2.1）

数据**完全复用 Bolt-2 Table 1**（公平对比），改进在：

1. **泄漏约束**：与 FEP+ benchmark 蛋白 ≥90% 序列相似的训练点全部移除
   （boltz2 只做报告划分，nesso 做了训练集剔除）。
2. **TerraBind 式矛盾过滤**：丢弃 H_PL > 0.7 且 pIC50 ≥ 6 的复合物
   （结构-亲和力自相矛盾：熵高却声称强结合 → 标签可疑）。
3. **z 掩码**：affinity 模块只见距配体 15Å 内的蛋白 token（口袋聚焦）。
4. **损失**：分类 focal loss；回归 Huber **拆成绝对项 + 相对差项，相对项
   加权**（显式优化 intra-assay 排序，抑制 inter-assay 偏差）。
5. Ki/Kd/IC50/EC50 **混用**（吸收公共数据的实验偏差与元数据缺失）。
6. 检查点平均（final + best-val 平均）。
7. **泛化关键**：frozen ESM2 嵌入替代 MSA（无 MSA 依赖）+ 蒸馏结构数据
   （AFDB 蛋白 + SAIR 复合物）多阶段（口袋裁剪渐进）训练。

### Nesso-1 的泛化评估方法（我们照抄）

- **化学相似度分桶**：按测试配体与训练集的 mean-max Tanimoto 分桶报
  Pearson——诚实展示"相似化学上性能好、低相似度上退化多少"。
- **OpenBind zero-shot**（EV-A71 2A 蛋白酶，494 化合物，训练集化学相似度
  极低）：nesso 胜过 Boltz-2/MW/cLogP 基线，但与 MW 简单基线差距不显著
  ——报告诚实承认公共数据训练的泛化极限。
- 结论引用：仅靠序列相似度约束泄漏时，BindingDB/ChEMBL 训练模型在更难
  assay 上显著退化（他们因此加了 H_PL 过滤 + 相似度分桶评估）。

## 三、我们的适配方案（p2d head）

我们的 head 消费 protenix trunk 表征 + pose 坐标，与 nesso 的
"trunk+独立 affinity 模块"同构。**推荐 nesso 数据配方**：

### 数据源（按可得性与优先级）

| 优先 | 源 | 规模 | 结构 | 获取 |
| --- | --- | --- | --- | --- |
| 1 | **PDBbind v2020 general+refined** | ~19k 复合物 | 晶体 pose + 亲和力 | pdbbind-plus.org.cn（注册） |
| 2 | **BindingDB**（SMILES+序列+亲和力） | ~1.14M 测量 | 无结构（trunk 共折叠生成） | bindingdb.org 批量下载 |
| 3 | ChEMBL v34（nesso 同版本） | 数百万 | 无结构 | EBI dump |
| 4 | Davis/KIBA（旧但干净） | 30k/118k | 无结构 | 公开直下 |

**路线 A（结构监督，PDBbind 起步）**：晶体 pose 训练 = 与推理（protenix
自生成 pose）存在小分布差；数据少但标签质量最高。**路线 B（nesso 式，
BindingDB/ChEMBL）**：序列+SMILES+亲和力，trunk 自己 co-fold——与推理分布
完全一致、量大；标签噪声大，必须全套策展。**建议 A 预热 + B 主训**。

### 质量过滤清单（prepare_affinity_data.py 实现）

boltz2 四层 + nesso 增补，全部落地：
- [ ] 测量类型 ∈ {Ki, Kd, IC50, EC50}；log10(µM) 标准化
- [ ] 温度标注 25-37°C（偏离丢弃，若有元数据）
- [ ] 单蛋白靶点；剔除 low-confidence 标记（ChEMBL confidence ≥ 7 若有）
- [ ] assay 内 std 过低（<0.3 log 单位）→ 丢弃该 assay（学不了差值）
- [ ] PAINS 过滤 + 重原子 ≤ 50 + MW ∈ [150, 900]
- [ ] H_PL 矛盾过滤（PDBbind 路线：熵从晶体接触面算）
- [ ] assay 级 MW 相关性检查：|Pearson(affinity, MW)| > 0.7 的系列降权/剔除
      （boltz2 论文精神：防"越大越强"伪相关）
- [ ] 去重：同 (靶点聚类, 标准化 SMILES) 取中位数测量

### 划分与泄漏控制（泛化核心）

- **蛋白聚类划分**：MMseqs2/CD-HIT 90% 序列相似聚类；train/val/test 按
  **簇**划分（同簇不跨集）——nesso 的 FEP+ 泄漏约束的推广。
- **化学泄漏**：test 配体与 train 的 max Tanimoto 分布要报告；理想上
  test 簇的 mean-max Tanimoto ≤ 0.4（OpenBind 式难集）。
- **时间划分**（有日期元数据时）：train 用旧数据，test 用新——模拟真实
  zero-shot。
- **评估必报**：整体 Spearman + 按 Tanimoto 相似度分桶的 Spearman
  （nesso Figure 7 协议）+ MW 基线对照（连 MW 都打不过就别上线）。

### 训练配置（承接 train_affinity.py）

- 损失：Huber(绝对) + λ·Huber(相对差)（λ=2 起）+ focal BCE；
  MC-dropout 打开（不确定性已在 head 内）。
- `--msa_prob 0.5`（已有）：MSA on/off 随机 → 无 MSA 推理可用。
- 检查点平均：保留 best-val + final 各一份，保存平均权重。
- 数据混洗按 **assay 分组**（同 assay 批内出现，利于相对差损失）。

## 四、风险与诚实预期

- OpenBind 上 nesso 也只与 MW 基线拉开不显著差距——公共数据训练的
  zero-shot 泛化有硬上限；我们的第一目标应定为 **FEP 式系列内排序**
  （benchmark 超过 cross-engine bridge 的 +0.40），zero-shot 为 stretch。
- 评估集固定用已有的 10 靶 FEP + cdk8（本仓库 benchmark），与 nesso/boltz2
  可直接对比；训练前先跑蛋白 90% 聚类确认 FEP 靶点不在训练集。
