# D-肽设计（镜像工作流）——工程文档

`peplm/dpeptide` 把经八轮实验验证的 D-肽镜像协议工程化为 V-Bio 生产代码。

## 1. 科学原理（镜像代数）

```
设计目标：  L-target + D-肽
等价问题：  D-target + L-肽（= 设计目标的镜像；用 L-世界工具求解）
最终步骤：  x → −x 翻转回 L-target + D-肽
```

镜像 `x → −x` 是几何精确的对映化：|φ_L + φ_D| = 0、|ψ_L + ψ_D| = 0、
CA 手性体积严格反号（D-α 螺旋 φ≈+57/ψ≈+47；L-α 为 −57/−47）。

**为什么必须镜像**：Boltz2/Protenix 的序列+MSA 输入手性盲，扩散生成先验
只会输出 L 坐标——端到端预测永远是 L-L 复合物。手性空间转换必须显式
做镜像，不能指望模型。

**为什么"对接阶段只做 diffusion"不可行**（E1 矩阵证据）：完美初始
0.00 Å 在无 MSA 固定受体精修下滑到 11.80 Å——扩散的"单序列链无关
先验"会主动破坏正确姿态。接口注册信息只能来自 trunk（MSA/共进化），
姿态一旦正确就只用 score 模式验证（坐标原样判分，零损伤）。

## 2. 模块

| 模块 | 职责 | 验证锚点 |
|---|---|---|
| `mirror.py` | 镜像/翻转、手性体积报告 | 双镜像=恒等；原生/镜像手性体积严格反号 |
| `pipeline.py` | `flip_product`：精修后镜像空间复合物 → 显示空间产物 | 3LNJ 夹具逐原子还原 |

> 历史：dihedral/placement/docking/manifest/scoring 及路线编排已随生产
> D 路线迁移至 `backend/runtime/run_single_prediction.py` +
> protenix2dock peptide 模式后删除，详见 `docs/peptide-design.md`。

## 3. 生产路线（历史记录，已被取代）

> 原"路线 A/B"（MSA 端到端 + 位姿镜像搬运 / de novo 单链构象 + 随机取向
> 放置）已被 backend/runtime 的统一 D 路线取代：上传结构 → x→−x 镜像
> D-target → 孤立构象口袋放置 → 固定 D-target inpainting → 翻转。
> 见 `docs/peptide-design.md`。

诚实边界：路线 B 产出为"正确口袋内的高置信替代模式"（~12 Å 注册滑移，
接触正确沟槽壁）；要晶体级注册走路线 A 或引入实验信息。

## 4. V-Bio 集成

- **引擎**：肽设计 backend 新增 `boltz2dock` / `protenix2dock`（结构对接
  语义；映射到 boltz2/protenix 全功能预测器补齐缺失结构）。
- **手性**：`peptide_design_options.peptideChirality = 'l' | 'd'`（D 需对接引擎）。
- **D 路线**（`run_single_prediction` 编排）：上传结构（或先单链预测）→
  x→−x 镜像成 D-target → PeptideLM 提案 → 逐候选孤立构象 → 口袋表面
  放置（`_dpeptide_stage_conformer_in_pocket`）→ 固定 D-target inpainting
  → composite → 精英循环 → 最优翻转+终验 → 与现有 result.zip 契约一致的产物
  （`structures/rank_NN.cif`、`structures/product_Ltarget_Dpeptide.pdb`、
  `results_summary.json` 含手性验证报告）。
- **口袋指定**（可选）：`peptidePocketCenter "x,y,z"` 或
  `peptidePocketResidues "A:101,A:102"`；缺省退化为靶链 CA 质心（文档明示）。

## 5. 测试

```bash
cd capabilities/peptide_lm
python -m pytest tests/test_dpeptide.py -q -m "not slow"   # 单测（CPU）
python -m pytest tests/test_dpeptide.py -q -m slow         # 端到端（GPU+docker）
```

单测覆盖：双镜像恒等、手性翻转（A/B 链）、镜像对 |φ+φ|=0、D/L-α 分类、
产物翻转恢复原生空间、验证器输出正确手性、序列提取、后缀剥离、口袋中心。
慢测：完整路线 B（构象→放置→对接→翻转→终验，断言 ipTM 与手性）。
