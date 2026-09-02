# Docking（dock 模式）机制与回归

## 机制：dock = 生成，不是精修

Boltz2Score 的 `dock` 模式把「SMILES → 3D 摆放 → 锚定扩散」当作**姿态生成**来跑，而不是对摆放结果的局部精修：

1. **摆放**：ETKDG 生成单个构象，重心平移到口袋中心（`--center_x/y/z`、`--pocket_ligand` 或
   `--pocket_residues` 定义口袋）。摆放只用来定义口袋接触约束（`anchor_contact_cutoff` =
   口袋半径），配体姿态由扩散搜索。
2. **生成调度**：`dock_default` 用引擎自己的全噪声生成调度
   （`sigma_max 160`，等效首步噪声 2560 Å）+ **200 去噪步 + 16 样本**（
   `core/flexible_optimization.py` 的 `MODE_CONFIGS`）。蛋白结构通过 input-init + self-template
   被钉在输入位置；配体被结构模块重新生成进口袋。
3. **为什么不能用精修调度（0.02–0.05）**：那组 sigma 是 pose/refine/interface 的校准值，
   对 dock 而言噪声太小（首步 0.8 Å），模型只能做局部抛光，配体卡在随机摆放的撞击姿态上。
   手调中间值（~0.3）虽在 CDK2 上有效，但对柔性配体会塌陷，所以不上锁任意值。
4. **姿态集（可选）**：`--dock_poses N` 为每个 SMILES 生成 N 个多样初始摆放（构象×朝向），
   全部精修后按 interface-aware 评分（`interface_rank_score`，ipSAE 为主）每配体保留最佳，
   淘汰记录移出、完整排名写入 `dock_ensemble_selection.json`。
5. **每配体只输出一个姿态**：档案里每个 SMILES 一个记录（含 `best_confidence.json` /
   `best_model.cif` / `dock_pose_diagnostics` 语义），前端的姿态集选择对用户透明。

## GPU 回归（CDK2 1H1Q，297 aa + 25 重原子配体，RTX 4090）

盲 dock（配体仅 SMILES + 原生口袋中心），对比原生共晶姿态：

| 度量 | 原生姿态（score） | 旧 dock（0.05/12/5） | 新 dock（160/200/16） |
| --- | ---: | ---: | ---: |
| 配体 pLDDT | 79.1 | 57.6 | **83.0** |
| ipTM | 0.969 | 0.854 | **0.972** |
| ipSAE_dom | — | 0.261 | **0.731** |
| affinity pIC50 | — | — | **8.64** |

- 蛋白刚性：输出复合物对齐输入蛋白 CA RMSD **0.38 Å**（模型靠 self-template 钉住蛋白）。
- 姿态质量：docked 配体距共晶结构最近原子均值 **0.93 Å**。
- 真实靶标复现（CDK8 5HNB + 吡唑并吲哚酰胺配体，前端提交的 p2d 同参数）：
  boltz2score dock pLDDT **86.8** / iptm 0.976 / ipSAE 0.723，与 protenix2dock 同任务
  （87.1 / 0.986 / 0.838）一致。

## 回归运行

```bash
# 1) CPU 单测（姿态集生成/命名/选优、共享排名度量等）
<boltz2score venv>/bin/python -m pytest capabilities/boltz2score/tests/test_dock_utils.py -q

# 2) GPU 回归（CDK2 盲 dock，~2.5 min）
cd capabilities/boltz2score
<boltz2score venv>/bin/python boltz2score.py --mode dock \
  --protein_file <cdk2_1h1q_chainA.pdb> \
  --ligand_smiles "Brc1cccc(Nc2nc(OCC3CCCCC3)c3nc[nH]c3n2)c1" \
  --center_x 0.89 --center_y 27.45 --center_z 8.07 \
  --output_dir /tmp/dock_regress --compute_ipsae --enable_affinity \
  --target_chain A --ligand_chain L --seed 42
# 期望 best_confidence.json：ligand_plddt_mean ≈ 83，iptm ≈ 0.972
```

## 为什么 score-mode 曾经“小分子 pLDDT 低”

低 pLDDT 是置信度头对**姿态质量**的正确打分，不是打分 bug（同一条打分管线：
原生姿态 79.1、质心平移 52.6、移出 8 Å 34.2）。修复前 dock 的摆放姿态本身带几十个重原子
clash（CDK2 上任意随机摆放 23–79 个 clash），而 0.05 调度无法让模型摆脱它。