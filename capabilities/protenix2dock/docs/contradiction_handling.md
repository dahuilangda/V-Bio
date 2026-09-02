# 矛盾数据处理研究：boltz-2 / nesso-1 的机制与我们的落地方案

用户问题：三类结构-亲和力矛盾数据如何处理——
- **A. 结合差 + affinity 低**（弱结构证据 + 弱实验值）→ 一致（非 binder）
- **B. 结合高 + affinity 低**（强结构证据 + 弱实验值）→ 表面矛盾，实际合法
- **C. 结合低 + affinity 高**（弱结构证据 + 强实验值）→ 真矛盾，最危险

---

## 一、boltz-2 的机制（论文 §Data + §Affinity training）

### C 类（弱结构+强亲和）：不直接过滤，靠鲁棒损失软处理
boltz2 **没有显式的 H_PL 过滤**，它用四层间接手段：
1. **assay 内方差下限**：亲和力标准差过低的 assay 整个丢弃——这些 assay
   无法提供 intra-assay 差值信号，只会引入 inter-assay 偏移噪声
2. **HTS 假阳性独立佐证**：二分类阳性必须在**独立 assay** 有定量测量
   （Ki/Kd/XC50）确认才保留 → 直接滤除"筛选判定结合但定量不复现"的 C 类
3. **focal loss**（二分类）：`-(1-p_t)^γ log(p_t)`，γ=2 时对高置信样本梯度
   ≈0 → 矛盾标签（模型判定为非 binder 却标注 active）的梯度贡献被抑制
4. **Huber loss**（回归）：小误差二次、大误差线性 → 单个坏标签的梯度贡献有界（相对 MSE 的平方增长）

### B 类（强结构+弱亲和）：双头分工，不冲突
架构上 binary head（结合与否）与 value head（亲和力数值）是**独立输出**。
分子可以占据口袋（binary=binder）但作用力弱（value=mM 级）——这不是标签错误而是物理事实（弱互补性或静电不利）。联合训练下两个头各自学习目标。

### 消除 inter-assay 偏差的核心设计：**assay 内成对差分损失**
```
L = Huber(value, label)            # 绝对项
  + λ·Huber(v_i - v_j, y_i - y_j)  # 相对差项，λ 加权（更强）
```
Cheng–Prusoff 论证：同一 assay 内 Ki/IC50 的差值抵消校正项 → 不同测量
类型可混合训练。矛盾数据的 inter-assay 偏移在差分中相互抵消。

### 采样策略
"balances binders and decoys while **prioritizing informative, high-contrast
assays**"——对比度好的 assay（活性/非活性都有分布）被优先采样；"Batches
are constructed to focus on **local chemical variation**"——同批放同系列的
化学变体（SAR），让相对差分有信号。

## 二、nesso-1 的增补：显式 C 类过滤

> "we discard any complex with protein-ligand entropy **H_PL > 0.7 and
> pIC50 ≥ 6**"

H_PL = trunk distogram 预测的蛋白-配体界面**熵**（TerraBind 启发式）：
- H_PL 高 = 模型认为配体位置弥散/无确定结合模式（弱结构证据）
- pIC50 ≥ 6 = 实验声称亚微摩尔结合（强实验值）
- 两者同真 = **标签可疑**（变构/共价/测定伪影/结合模式结构未捕获），
  训练它们会教模型**无视结构证据** → 整条丢弃

这是 nesso 相对 boltz2 的关键差异（论文原文："A key distinction, however,
is how structure-based filtering is implemented in practice"）。

引用支持：模型仅靠序列相似度约束时在更难 assay 上显著退化（[59]），
H_PL 过滤是针对性的补丁。

## 三、机制汇总表

| 矛盾类型 | boltz-2 | nesso-1 | 效果 |
| --- | --- | --- | --- |
| A 弱+弱（一致） | 合成 decoy 扩充 + focal 学"非结合" | 同左 | 正常负例 |
| B 强结构+弱亲和 | 双头分工（binary/binary 值独立） | 同左 | 合法物理事实 |
| C 弱结构+强亲和 | assay 方差地板 + HTS 独立佐证 + focal/Huber 软抑制 | **+ H_PL>0.7∧pIC50≥6 硬过滤** | 硬删标签可疑数据 |
| inter-assay 偏移 | assay 内成对差分损失（λ 加权） | 同左 + Huber 拆绝对/相对 | 偏移在差分中抵消 |
| 单点坏标签 | Huber（大误差线性） | 同左 + 检查点平均 | 梯度贡献有界 |

## 四、我们的落地方案（protenix2dock affinity）

### 已实现（此前轮次）
- ✅ focal(γ=2) + Huber + assay 分组相对差分（`--rel_weight`）
- ✅ assay 方差地板 + MW 相关 assay 过滤（策展）
- ✅ MC-dropout 不确定性（affinity_pred_std）

### 本轮新增（H_PL 矛盾过滤，nesso 式）
训练时对每个样本从 trunk distogram 算 H_PL：
```
H_PL = mean over ligand tokens of entropy(distogram(lig_token, pocket_prot_tokens))
```
- H_PL > 0.7 且 label pIC50 ≥ 6 → **跳过该样本**（nesso 硬过滤）
- H_PL > 0.7 且 pIC50 < 6 → 保留（A 类一致：模型说弥散 + 实验说弱，正常）
- 同时统计 contradiction 率输出到日志（数据质量监控）

实现要点：distogram softmax 概率的 Shannon 熵（对 lig-rec token 对取均值），
在 `_FrozenTrunk.representations` 里已算 expected_dist 的地方顺手算，
零额外前向开销。

### 后续可选（boltz2 式软处理）
- per-sample 损失截断（small-loss trick：每 batch 丢弃损失最高的 k% 样本，
  对抗标签噪声的标准做法）
- high-contrast assay 优先采样（按 assay 内 pIC50 方差加权采样器）
