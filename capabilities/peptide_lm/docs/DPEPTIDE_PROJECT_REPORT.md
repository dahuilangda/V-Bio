# D 肽项目报告（历史记录）

> **已被取代**：生产 D 路线现为 backend/runtime 的镜像-口袋-inpainting 编排，
> 机制与验收见 `docs/peptide-design.md`（本目录文档中提及的
> `_run_d_peptide_design_loop`、`design_d_peptide_route_b`、placement/scoring
> 模块已删除；`dpeptide_dock_task` 备用任务亦已移除）。


**日期**：2026-08-26
**范围**：八轮研究结论 → 生产工程包 → V-Bio 全栈集成（后端/worker/前端）→ 测试与 review
**研究档案**：八轮完整证据链（REPORT.md 及实验产物）保留在开发环境的研究目录，不随仓库分发
**生产代码**：`/data/V-Bio/capabilities/peptide_lm/peplm/dpeptide/`（唯一维护位置）

---

## 一、研究结论摘要（八轮，全部有实测锚点）

### 1. 镜像协议可行性（已证明）
| 命题 | 实测 |
|---|---|
| 镜像是对精确对映化 | 原生/镜像对 \|φ+φ\|=0.0、\|ψ+ψ\|=0.0；CA 手性体积严格反号（±2.5） |
| Boltz2 置信头兼容 D 几何 | 镜像复合物 score 模式 ipTM 0.935-0.948（3LNJ/8F10） |
| D 残基名必须重命名 | 不重命名（→UNK）ipTM 崩溃为 0 |
| 翻转闭环 | score 直通 0.000 Å；产物 φ/ψ 与原生逐残基同家族（D-α） |

### 2. 架构归因（trunk vs diffusion）
- **序列+MSA 手性盲**：端到端预测永远是 L-L 复合物；手性转换必须显式镜像。
- **diffusion-only 天花板**（E1 矩阵）：完美初始 0.00 Å → 无 MSA 固定受体精修 11.80 Å（主动破坏姿态）；MSA 抵抗到 6.5-7.2 Å；**trunk 主导才到 2.36 Å**。
- **MSA 分水岭**（P1-P3）：无 MSA Protenix ipTM 0.43-0.55（肽 17-24 Å 失败）；unpaired MSA 即 0.94-0.96（受体 0.30 Å、肽 2.36 Å 残基级注册）。
- **正确姿态只用 score 验证**（E6）：镜像搬运 pose 判分 ipTM 0.892、iRMSD 2.03 保持（零损伤）。

### 3. 工作流定型
```
路线 A（MSA 可得）：Protenix+MSA 端到端（ipTM≥0.9 门控）→ 位姿镜像搬运 → Boltz2 score 验证（不重扩散）→ 翻转 → L-target+D-肽
路线 B（de novo）  ：肽序列 → 单链构象（Protenix 无 MSA，"肽版 ETKDG"，1.70 Å）→ 口袋随机取向放置 → 固定 D-target box 对接（ipTM 0.95+）→ 翻转 → 产物+φ/ψ 终验
```
诚实边界：路线 B 产出为口袋内高置信替代模式（~12 Å 注册滑移）；晶体级注册需路线 A 或实验信息。

### 4. 排查中修复的缺陷（研究阶段，全部记录在案）
Kabsch 转置、逐残基 uid 误当链 id、box 全系统平移被重心归零抵消、镜像后重扩散破坏 pose、L/D 参照系错配、one-hot 误读（曾致一次误报上游 bug，已勘误）。**生产包只含修正后的正确实现。**

---

## 二、工程化交付

### 1. `peplm/dpeptide` 包（7 模块，只含验证过的正确代码）

| 模块 | 职责 | 关键实现要点（源自踩坑修复） |
|---|---|---|
| `mirror.py` | 镜像/翻转、D→L 名、手性报告、口袋中心 | 精确 x→−x；词表映射表；wat/帽残基清理 |
| `dihedral.py` | φ/ψ 与 L/D Ramachandran 分类、产物终验 | 镜像对判据 mode='mirror'（和≈0）/同手性 mode='same'（差≈0） |
| `placement.py` | 构象生成（docker Protenix 无 MSA + 理想螺旋回退）、口袋随机取向放置 | 均匀随机轴角 proper rotation |
| `docking.py` | 固定 D-target inpainting 采样器 | 链 id 取自 token 级 asym_id（非逐残基 uid）；无增广稳定帧；受体 noisy/x0/Euler 三处硬重置；已知原子零噪声（RePaint）；box 平移只作用于肽原子 |
| `manifest.py` | manifest 级 record 手术 | **数据模块只读 manifest.json**（改 records/*.json 无效）；聚合肽口袋条件化（生产版仅接受 NONPOLYMER，此处支持肽） |
| `scoring.py` | score 验证 + dock 驱动 | 模型单次加载复用；score 模式零坐标损伤 |
| `pipeline.py` | 路线编排 | target 缺失先预测（boltz/protenix 全功能）；产物翻转+φ/ψ 终验 |

### 2. V-Bio 后端
- `routes/prediction.py`：`boltz2dock`/`protenix2dock` 仅对 `peptide_design` 工作流合法（400 拒绝其它工作流）；legacy 值不受影响。
- `runtime/run_single_prediction.py`：
  - `_is_docking_peptide_backend` 识别对接引擎；`_normalize_peptide_backend` 将其映射到全功能引擎（**集成测试抓到 protenix2dock 曾错映射到 boltz，已修**）；
  - `peptideChirality` 选项解析（'l'/'d'；D 强制要求对接引擎，否则 ValueError）；
  - `_run_d_peptide_design_loop`：上传结构 or **先预测 target**（boltz2dock→boltz2、protenix2dock→protenix，互为回退）→ 镜像 → PeptideLM 提案循环 × 逐候选 D-oracle（构象→放置→固定 D-target 对接→composite）→ 最优翻转+手性终验 → 按现有 result.zip 契约产出（`structures/rank_NN.cif`、`structures/product_Ltarget_Dpeptide.pdb`、`peptide_design_summary.json` 含 `product_chirality_validation`）。
  - 口袋指定：`peptidePocketCenter "x,y,z"` / `peptidePocketResidues "A:101,..."`（镜像帧自动换算）；缺省靶链 CA 质心（文档明示退化）。

### 3. 前端
- 肽设计 backend 下拉：**Boltz2Dock / Protenix2Dock**（+AlphaFold3 保留）；预测/亲和工作流不变。
- **Peptide Chirality** 选择器（L 标准肽 / D-肽·镜像工作流；D 仅在对接引擎可选）。
- 管线全通：types → editorActions → handlers → workspaceView → sectionProps → UI；`PredictionBackend` 类型与两个归一化器均支持新 token。
- `npx tsc --noEmit` **零错误**。

### 4. 测试（47/47 通过，无回归）
```
tests/test_dpeptide.py            12 单测（CPU）：镜像恒等/手性翻转/|φ+φ|=0/D-α 分类/产物翻转/终验器/工具
tests/test_dpeptide_integration.py 10 集成：dock token 校验/引擎映射/靶序列提取/口袋解析/前端契约
tests/test_dpeptide.py::slow       1 端到端（GPU+docker）：构象→放置→对接→翻转→终验
原有套件                          全部通过（无回归）
```
集成测试的价值实证：抓到 `protenix2dock→boltz` 错映射（会导致 D-loop 走错引擎队列）。

### 5. 文档
- `docs/DPEPTIDE_DESIGN.md`：原理/模块/路线/集成/测试（生产参考）。
- 八轮研究档案（REPORT.md、实验产物、复现脚本）保留为证据链；生产代码以 `peplm/dpeptide` 为唯一维护位置，研究目录不再演进。

---

## 三、Review 记录

| 检查项 | 结果 |
|---|---|
| worker 模块加载（含全部新函数） | ✅ |
| 后端路由语法 + 校验顺序 | ✅（ast + 逻辑镜像测试） |
| 前端 tsc | ✅ 零错误 |
| 单测 + 集成 + 原有回归 | ✅ 47/47 |
| 引擎映射正确性 | ✅（集成测试修复后） |
| zip 产物契约与现有 worker 一致 | ✅（rank_NN/summary 字段对齐，附加 chirality 字段向后兼容） |
| E2E（GPU 全流程） | ✅ 通过（12:15，ipTM 0.942，产物 D-α 9/9，见附录） |
| 降级路径 | 构象生成 docker 失败→理想螺旋回退；结构预测 boltz↔protenix 互为回退 |

## 四、已知边界与后续
1. 路线 B 注册滑移（~12 Å）为模型能力边界（trunk 接口先验缺失），文档已明示；建议产线 D-肽先走路线 A。
2. `predict_target_structure` 的 boltz 路径用 `boltz.main predict` CLI（首次使用建议人工验证一次输出路径约定）。
3. Protenix 训练集可能含测试结构（leakage），严格盲测需训练截止后结构（研究档案第五轮已注明）。
4. E2E 慢测建议纳入夜间 GPU 冒烟。

---

## 附录：E2E 冒烟实测（工程包全流程，2026-08-26）

`pytest tests/test_dpeptide.py -m slow`（GPU，12 分 15 秒，1 passed）：

```
3LNJ L-target →(镜像)→ D-target → 肽序列 SWYASLEKLLR
  → Protenix 单链构象（无 MSA）→ 口袋随机取向放置
  → 固定 D-target box 对接 → 翻转 → 产物 + 终验
```

| 指标 | 值 |
|---|---|
| 对接置信 | **ipTM 0.942 / confidence 0.934** |
| 产物受体手性 | L ✓（CA 体积 +2.52） |
| 产物肽手性 | **D ✓（−2.46）** |
| 产物肽构象 | **D-α 螺旋 9/9**（φ 均值 +65.2，与原生 D-PMI +64.2 同家族） |
| vs 原生 φ 残差 | 15.7°（已知注册滑移层面，非手性/构象类型错误） |
| 产物 | `PRODUCT_Ltarget_Dpeptide.pdb`（L-target + D-肽） |

工程包与八轮验证的研究协议行为一致（ipTM 0.94-0.96 区间、D-α 产物），
生产化完成。

## 附录二：前端联调修复（用户实测反馈）

首版集成后用户报告"Peptide Chirality 里 D-peptide 永远灰"。两个真实缺陷，均已修复并重建：

1. **过期构建产物**：源码已含新 UI 但 `dist/` 是旧构建（11:21）→ 已重新 `vite build` 并重启 preview（5173），服务端 bundle 经 curl 断言含 `Boltz2Dock`/`Protenix2Dock`/`Peptide Chirality`。
2. **select 值与选项集失配**：肽设计 backend 下拉仅提供三个引擎，但存量草稿默认值是 `'boltz'` —— React select 无匹配项时浏览器"显示第一项、实际 state 不变"，导致看似选中 Boltz2Dock 而 D 仍禁用。修复：进入肽设计工作流时若 backend 不属于 {boltz2dock, protenix2dock, alphafold3}，自动迁移为 'boltz2dock'（useEffect 单次触发）。
3. **options 白名单剥离 peptideChirality**：`normalizeProjectInputConfig` 按白名单重建 options（提交与项目加载都会走），未登记的字段被静默丢弃 → 已在解析、两个返回分支和默认值五处登记 `peptideChirality`。

复验要求浏览器强制刷新（Ctrl+Shift+R）。操作路径：多肽设计 → Backend 选 Boltz2Dock 或 Protenix2Dock（现在会自动选中）→ General 组 Peptide Chirality 选 D-peptide。

## 附录三：深度 review + 真实测试（第四轮迭代，用户指令"不要fallback/兜底"）

### 移除的 fallback（根源化）
| 位置 | 原兜底 | 现行为 |
|---|---|---|
| placement.protenix_conformer | docker 失败→理想螺旋构象 | **删除**：抛 RuntimeError（含镜像镜像要求说明），任务失败即失败 |
| worker 目标结构预测 | boltz↔protenix 互为回退循环 | **删除**：单引擎策略——boltz2dock→Boltz-2、protenix2dock→Protenix；失败抛明确错误 |

### Bicyclic "消失"问题的终审
构建产物与服务端 bundle 经逐层断言均包含 `value:"bicyclic"` 与全部模式选项；运行时消失感源于两个真实缺陷：
1. select 值失配：backend 默认 'boltz' 不在新选项集 → 浏览器显示第一项而 state 不变 → 已加自动迁移 effect。
2. 首次落地的 D×cyclic/bicyclic 组合约束只存在于后端报错，UI 未表达 → 现已双向约束：Chirality=D 时 Cyclic/Bicyclic 选项置灰并标注 "(D-form: Linear only)"；反之在 D 激活时切换到 Cyclic/Bicyclic 会自动把 Chirality 复位为 L（editorActions 状态同步）。后端守卫保留为权威校验，文案与 UI 对齐。

### Review 修复清单（本轮）
R1 D+非 Linear 后端守卫（文案对齐 UI）｜R2 多模板确定性说明｜R3 _parse_confidence 死代码+空 payload 守卫｜R4 sys.path 注释修正｜R5 fallback 螺旋整段删除｜R6 docking.py 死导入清理｜R7/R8 构象与结构预测按 ranking_score 选最优样本｜R9 projectTaskSnapshot 持久化 peptideChirality。

### 本轮新增测试与结果
- 属性测试：随机 SE(3) 刚体变换 ×20 —— 手性体积/dihedral 不变性；随机镜像反号。15/15 CPU 单测通过。
- 真实 GPU 推理（工程化 score_complex）：native 直通与搬运 pose 两用例（slow 标记，前一进程已被会话中断重启，见下）。
- 全量回归：50 passed（tests/ -m "not slow"），tsc --noEmit 零错误，vite build 成功并被 5173 服务确认。

## 附录四：真实推理测试结果（最终）

`pytest TestRealInference -m slow`（GPU，66.5 秒）：**2 passed**
- test_score_native_passthrough：原生复合物经工程化 score_complex 直通验证，ipTM>0.85 断言通过
- test_score_transferred_mirror_pose：镜像搬运 pose（2.36 Å 初始）工程化 score_complex 验证，ipTM>0.8 断言通过

至此深度 review 闭环：后端路由/worker、dpeptide 七模块、前端五处接线全部人工逐行审查；R1-R9 缺陷修复；三层测试（15 单元属性 + 10 集成契约 + 2 真实 GPU 推理 + 50 回归）全绿。

## 附录五：深度 review 第四轮（真实集成测试 + Copilot/文案/Affinity 同步）

### 后端 API 真实可用性（Flask test_client 直打 /predict）
| 请求 | 结果 |
|---|---|
| dock 引擎 × prediction | 400「仅 peptide_design 可用」✓ |
| 缺 yaml_file | 400 ✓（auth 校验后第一道闸） |
| **boltz2dock × peptide_design** | **202 进入编排** ✓ |
| **protenix2dock × peptide_design** | **202 进入编排** ✓ |

测试脚本固化：`scripts/flask_route_contract.py`（V-Bio venv 运行，带 API token 头，从 config 读取）。

### Backend 点击无响应 —— 根因修复+复演证明
esbuild 转译 editorActions 后用 Node 直接驱动生产代码状态机：点击 Boltz2Dock 后 `draft.backend==='boltz'`（被吞）。根因是 `projectDraftUtils.normalizePredictionBackend` 的独立副本不认识 dock token。修复该源并复演：四次连续用户操作全部得到正确终态（boltz2dock→d→bicyclic自动复位l→回linear+d保持）。

### 其它归一化副本清剿
copilot/snapshot 补丁守卫（useProjectDetailWorkspaceView 两处 backendPatch 白名单）、apiAccessHelpers、workspaceViewHelpers.strict、projectDraftUtils 全部登记 dock token；projectLoadFlow 经共享 normalizer 自动获得支持。

### Affinity(dock) 模块同步
AffinityWorkspace backend 标签切换为 Boltz2Dock/Protenix2Dock（值保持 boltz/protenix 兼容既有路由与 worker）。

### Copilot 适配
确认卡 `parameterPatch` 摘要由“裸键名列表”升级为友好标签渲染：backend→Boltz2Dock/Protenix2Dock、peptideChirality→Chirality: D-peptide/L-peptide、mode→Linear/Cyclic/Bicyclic。

### 前端长句文案清理
移除肽设置中冗长的引擎说明段落；残基池快照提示压缩为 "Edits apply to the next submission."

### 回归
- peplm 全量 CPU：50 passed
- Flask 路由契约：RUN 手动通过并固化脚本
- tsc --noEmit：零错误；vite build 成功；preview 已验证服务新 chunk

## 附录六：线上 400 终审（stale API 容器）+ 线上验证 + 事故处置

用户线上报 `400 Invalid backend 'protenix2dock'`。逐层定位：
- **端口 5000 = `vbio-central-api` 容器**（8-24 创建）。代码是 bind-mount 的最新源，但 Python 进程自创建起未重启 → 路由校验持有旧逻辑 → 吞掉 dock token。
- 已执行 `docker restart vbio-central-api`。重启后 curl 断言：
  - dock token × prediction → 新文案「仅 peptide_design 工作流可用」✓
  - protenix2dock × peptide_design(chirality=d, 1代×1候选) → **202 入队成功** ✓

### 兼容层（无需等待全平台重启）
前端提交时将 dock 引擎映射为全功能引擎值（protenix2dock→protenix / boltz2dock→boltz），对接语义由 `peptideChirality='d'` 携带；worker 侧同步放宽"必须 dock token"死限制。因此：**新代码**原生接受两种写法；**旧容器/服务未重启时**映射后的请求也可被旧路由+现 worker 正确处理。

### 事故处置记录
探针期间误入队两个真实任务：1×1（已 revoke ✓）与默认 12 代×16 候选大任务（959dd9fd…，已 revoke ✓）；GPU 与任务容器确认归零。另确认主机上高 CPU 的 precompute_feats/train_affinity 为无关长跑训练，未受影响。

## 附录七：引擎语义统一（用户终版定义）+ 前端启动修复

### 引擎语义（按用户终版定义）
- **多肽设计 L/D 一律提交 `boltz2dock` / `protenix2dock`**（不再映射为 boltz/protenix）。
- 仅当 **target 未上传结构** 时，才由 worker 的 D-loop 用对应完整引擎（Boltz-2 / Protenix）预测 target——这一步在 D-loop 内部完成，不经路由 backend。
- 已删除此前加的"wire 映射层"；前端 `effectiveBackend = draft.backend` 直发 dock token。调度层补齐 `boltz2dock → boltz2` capability 别名（protenix2dock→protenix 队列宿主映射原已存在并有注释）。

### 前端起不来根因
`npm run build = tsc -b && vite build`：此前我手跑的是裸 `vite build`（跳过类型检查），掩盖了 **5 个 TS 类型错误**（Result 接口缺 chirality handler、backendMirror 状态类型、mode action 条件展开把 chirality 拓宽成 string 等）。全部修复后：
```
npm run build    ✓ built in 12.44s
frontend/run.sh restart → frontend http:200（preview 正常服务）
curl 断言服务 chunk 含 Boltz2Dock/Protenix2Dock/Peptide Chirality/Bicyclic ✓
（注意 grep 服务端 gzip 响应会得 0，需 -H 'Cache-Control: no-cache' 或落盘比对）
```

### 验证汇总（本-appendix）
tsc --noEmit 零错误｜npm run build 完整通过｜run.sh restart 后 :5173 HTTP 200 且服务最新 chunk｜API 重启后 dock×prediction 新文案、peptide_design 接受 dock token（此前已验 202）

## 附录八：原理终审（环化与镜像正交）+ 引擎语义统一落地

### "Linear-only" 限制被推翻（用户指正正确）
从第一性原理：**环化是标量距离拓扑约束（头尾酰胺键、3×Cys-linker），x→−x 变换保持所有标量距离不变**——D-cyclic/D-bicyclic 与镜像工作流完全正交、可自由组合。此前 "D 仅 Linear" 的三处限制（后端守卫、UI 置灰、模式切换自动复位）已全部移除；后端守卫改为注释说明其正交性。NCAA 同理：CCD modifications 走 Boltz YAML 生产协议，手性无关。

### D-oracle 打分架构定型（用户指令："不用 D-loop，是 D-target 设计 L-肽"）
```
编排(任意 worker)     : PeptideLM 提案 → 候选序列(+mods/cys)
构象源               : protenix2dock 单链预测（linear 快路径，无 MSA）
                       cyclic/bicyclic → L-oracle YAML (boltz predict, 含键约束/NCAA)
D-oracle 打分        : 子进程 python -m peplm.dpeptide.cli
                       （进程内固定 D-target 采样器+镜像放置，编排器保持 torch-free）
翻转                 : mirror-back → L-target + D-肽产物 + φ/ψ 终验
```

### CLI 首次真实 GPU 冒烟通过
`python -m peplm.dpeptide.cli` 于 staged D-target 复合物：**ipTM 0.927 / confidence 0.931**
（置信 JSON 已由 CLI 输出至 out_dir，包含全部指标供 composite 使用）

### 回归
tests/ 全量 CPU **54 passed**；前端 tsc 零错误。

## 附录十：收尾冲刺（BS3 恢复 / Backend 点击修复 / 线上 400 终审 / flip 缺口如实呈现）

### BS3 "Linker Type 删了 Bi" 恢复（用户报告）
git 工作区存在未提交的破坏性变更：`linker_ccd/BS3.cif` 被删、`regenerate.py LINKERS` 去掉 BS3、前端 `BicyclicLinkerType/BICYCLIC_LINKERS/VALID set` 收窄为 SEZ|29N。全部按 `9b6ffea/1f475ac` 历史恢复：后端 atom map `BS3: [BI,BI,BI]`、BS3.cif、pkl、regenerate；前端类型/列表/白名单。工作区 diff 中无本会话产生的删除痕迹——判定为早前某次本地操作遗留，非我引入，但由本轮负责任复。

### Backend 点击选择没反应 —— 根因（esbuild 状态机复演证明）
`projectDraftUtils.normalizePredictionBackend`（editorActions 实际使用的第 5 个归一化副本）不认识 dock token → 点击被吞回 'boltz'。已修复并经 Node 直接驱动生产代码状态机四次连续用户操作复演全对。（此前 apiAccessHelpers/workspaceViewHelpers 两处已在更早轮次修复。）

### 用户失败提交 c425236c（bicyclic+BS3+D）线上复盘
依次修正了三层问题：stale central-api 容器（重启即好）、GPU 队列误派（撤回 chirality GPU 直派 hack）、CPU 编排 worker 无 Boltz2Score venv（target 预测外派 cap.boltz2/cap.protenix 队列的 predict_task）。

### flip 展示层的已知缺口（如实）
编排 worker 内不打分也不持有模型 —— zip 中 `structures/product_Ltarget_Dpeptide.pdb` 的翻转生成需要 top 复合物的**本地结构文件**；当子任务仅上传中央 API 而编排层拿不到结构路径时 flip 记为 skip 并写明原因。补齐方式已在文档建议：候选评估子任务额外回传结构文件至共享盘（一行 mount/协议扩展），非架构问题。

### 最终验证汇总
- peplm 全量 CPU 回归 54 passed
- 前端 tsc/npm build 全绿；预览服务断言含 Boltz2Dock/Protenix2Dock/Peptide Chirality/BS3
- live：protenix2dock×linear×d 入队→完整 D-oracle 循环跑通（镜像打分子任务 cb5 外派成功）；bicyclic×BS3×d 入队→标准管线完成出分 0.906（flip 显示层如上属已知缺口）

## 附录十一：GPU 利用率验证 + 最终工程形态定稿

### GPU 池并行度实测（boltz2dock × linear × D × 4候选并行）
e52f16b1 任务：proposer → 4 个候选子任务经 celery 派发 → GPU worker 并行执行 → 全部完成（31.5s / 31.8s / 60.7s / 61.2s，总窗口内 4/4 SUCCESS）。并发数由 `_resolve_peptide_parallel_workers` 自动取 min(population, GPU池容量)，池容量来自 gpu_manager（当前 available=2，另有 2 卡被无关训练占用）。

### 最终工程形态（重要更正）
此前多轮引入的「编排容器内 torch 推理 / 固定受体采样器 / celery 嵌套外派」属于过度设计——反复踩坑（GPU worker 无 torch / CPU worker 无 docker.sock）的本质原因正是偏离了平台正统管线。最终形态（全部复用已验证的生产组件，零进程内 torch 于编排）：

```
chirality=d 的多肽设计 = 标准 L 流程编排 + 展示层镜像
  proposer(CPU) → 候选评估子任务(GPU 引擎容器, 原生 linear/cyclic/bicyclic/NCAA) → composite 排名
  → 展示层：best 复合物翻转(PRODUCT_Ltarget_Dpeptide.pdb) + φ/ψ/手性验证
```

### 本轮 review 清理项
- 删除错误引入的 `_candidate_conformer_path`（15 行复杂分支）
- `backend/worker/dpeptide_dock_task.py`：保留为备用任务模块（standby，不在派发路径），报告注明
- scoring.py 根目录/cache 参数化（env 可覆盖，供不同部署位形）

### 支持矩阵（终版）
| | Linear | Cyclic | Bicyclic(+BS3) | NCAA |
|---|---|---|---|---|
| **L-肽** | ✅ | ✅ | ✅ | ✅ |
| **D-肽（同管线+展示翻转）** | ✅ | ✅ | ✅ | ✅ |
实例证据：bicyclic×BS3×chirality=d 全链路（proposer→预测→打包）9576ce2b/e52f16b1 与用户场景同构的任务均成功出分出结构。

### 已知缺口（如实）
1. 展示 flip 需要 top 结构本地文件可见性——当候选子任务只上传中央时编排拿不到 path，flip 记录 skip 原因（transparent，不入静默失败）
2. 训练占用的 2 张卡不计入可用池（符合预期隔离）

## 附录十补：79332a4f/b6576479 失败终审（bicyclic+NCAA 池 10 项首例全组合）

### 根因（非手性、非管线架构）
Protenix 引擎在加载自定义 CCD ligand 时，`chem_comp_bond` category 为空数组，
biotite 抛 `Array must contain at least one element` → DeserializationError
→ `Protenix finished without a renderable structure`。

发生位置：引擎数据加载（json_parser.build_ligand → ccd.get_component_atom_array）。
触发物：候选肽中的某个 NCAA 自定义 CCD（任务池含 AIB/NLE/NVA/ORN/CIT/HSE/HCY/MSE/SEC/HYP 十项 + SEZ linker；SEZ 本身已在其它成功双环任务中验证过键约束正常，最可疑的是新引入 NCAA 中某个在 `custom_ccd_builder.build_ligand_ccd_mmcif` 输出时 bond 表为空/缺列——例如仅单原子片段或 RDKit 感知键级缺失的条目）。

### 为什么之前没发现
这是 chirality=d × bicyclic × NCAA 池 10 项 全组合的首个真实端到端（E5 冒烟只用天然序列）；而 L 流程生产此前未覆盖这批 NCAA 与 bicyclic 同提的组合。

### 修复方案（下一步 TODO，三处防线）
1. `custom_ccd_builder`：生成后自检 `_chem_comp_bond` 行数>0 或原子数==1，否则 raise 带 CCD code 的明确错误（fail fast at build）。
2. `boltz2score/protenix` 引擎报错时把触发 CCD code 写入 stderr summary（当前只有裸 category 名，无法定位是哪个残基）。
3. 提交入口对用户 NCAA 池做一次「dry-run 解析」（build+cif 解析即弃），把解析失败的 NCAA 在 400 响应里指名道姓，避免耗完 GPU 才失败。

## 附录九合并说明
调度事实链（859b GPU 队列无 torch→撤回直派 hack；打分外派链路 cb5/cb7 成功上报）如附录九；本轮 b6576479 为另一独立缺陷（自定义 CCD 数据面），两案互不掩盖。
