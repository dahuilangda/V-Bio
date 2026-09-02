# Backend 接线（PeptideLM 为唯一设计引擎）

`backend/runtime/run_single_prediction.py` 的肽设计路径只使用 PeptideLM
提案引擎（`peplm/integrate/backend_proposer.py`）。无回退、无兜底：
初始化、propose、learn 任一失败都会以任务错误上报。

## 提案引擎（每代）

proposer 直接产出 (base_sequence, modifications) 对：

- 序列来自 Tier1 先验的 RL 副本（agent），de novo 条件采样 + FIM span 编辑
  + 点突变；每代结束后 GRPO 在线学习。
- **长度**：用户未设置 `peptideBinderLength` 时自适应探索（10-30），
  设置时固定为该值（均为解码保证，不再裁剪）。
- **固定残基**：`peptideSequenceMask` 字母位置转 `fixed_residues`
  （解码期强制）。
- **NCAA 严格用户池**：`peptideResiduePool` 非天然条目 + 自定义 CCD →
  `ncaa_pool`；未选任何非天然条目 → 纯天然设计（池外 token 解码禁用）。
- **双环 Cys**：`peptideBicyclicCysPositionMode=manual` 时 Cys1/Cys2 作为
  内部锚点；auto 时取中点；首位与末位恒为 Cys（first/interior/last）。
  掩码第 1 位如被固定为非 C 会在任务开始前报错。
- **`peptideNcaaDecodeBias`**（可选环境级旋钮，缺省 0.5）：解码期向用户
  池的软偏置。

## 后端边界

| 后端 | linear | cyclic | bicyclic | NCAA（线性路径）|
|---|---|---|---|---|
| boltz | ✅ | ✅（原生 `cyclic:true`）| ✅（SEZ/29N 键约束）| ✅ |
| protenix | ✅ | ✅（显式 N-C 键 → covalent_bonds）| ✅（3×SG↔linker 键）| ✅ |
| alphafold3 | ✅ | ❌ 后端拦截报错 | ❌ 后端拦截报错 | ✅（custom CCD mmCIF）|

AF3 拦截发生在任务早期（任何 GPU/候选工作之前）；前端同步将
cyclic/bicyclic 选项置灰。三个后端对非天然氨基酸的支持已全程走通
（boltz: BOLTZ_CACHE/mols pkl；protenix: CCD_ 修饰 + 公共缓存 overlay；
alphafold3: ptmType 修饰 + userCCD 块）。

## 前端

`WorkflowRuntimeSettingsSection`：Design Mode 选择（AF3 下禁用环/双环）、
Peptide Length（可选，`Adaptive length` 开关 → 不发送 binder_length，
后端自由探索）、Residue Pool/自定义 CCD、双环 Cys 位置（auto/manual）、
固定残基掩码。

部署注意：先验位于 `capabilities/peptide_lm/models/prior.pt`（附
`models/MANIFEST.json`）；采样设备由 `VBIO_PEPTIDELM_DEVICE` 控制（默认
cpu）；可用 `VBIO_PEPTIDELM_PRIOR` 覆盖先验路径。