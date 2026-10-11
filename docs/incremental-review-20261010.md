# 增量报告：L/D 双环肽结果展示修复 + 工程/性能/视觉迭代（多轮独立 review 收敛）

- 日期：2026-10-10
- 方式：视觉走查（无头浏览器逐页截图 + DOM 取证）+ 数据取证（归档/DB/渲染三方比对）+ 三路并行独立审查 agent（后端正确性 / 性能网络 / 前端 UX）+ 两轮回执复检
- 范围：`backend/routes/task.py`、`backend/runtime/{run_single_prediction.py,peptide_archive_layout.py}`、`backend/monitoring/result_cleanup.py`、`backend/services/result_archive.py`、前端 resultParser/结果工作台/懒加载/Copilot 会话态；`capabilities/` 与 `vendor/` 的改动系会话前他人 WIP，不在本报告范围

## 一、CD73 项目 L/D 双环肽结果展示错误（用户报告）——三方证据链闭环

**症状**：v11 批次（9 靶点 × L/D）任务分数远低于实际、候选列表乱序且混入全部淘汰候选、L 系列候选几乎全部"无结构"、任务卡数字与归档不一致。

| # | 根因 | 证据 | 修复 |
|---|---|---|---|
| 1 | **worker `all_results` 从不排序**（9971 行注释声称"sorted on every append"但无排序）→ 进度里的 `best_score=all_results[0]`、`current_best_sequences=[:10]`、`design_results.json` 全部冻结在"最早完成的 gen1 候选" | 7 个任务 DB 分数与 design_results[0] 逐位一致（7/7） | 追加后按 `_peptide_rank_score` 重排；design_results 随之有序 |
| 2 | **前端解析器"最长数组获胜"**：design_results.json（完成序全量 dump，63-162 行）压过 results_summary.json 的精选 top_results（24 行）；且把选中数组 [0] 的 iptm/plddt 提升为任务级分数 | DB 顶层 iptm/plddt 与 dump 首行逐位一致 | `selectPeptideCandidateRowsFromSummaries`（纯函数+4 直测）：canonical `top_results` 优先，legacy 回退才比长度 |
| 3 | **L 路线打包无结构回退**：checkpoint 断点续跑 + tmp 清理后原始结构丢失（D 路线有 dpeptide_keep 回退，L 路线没有） | 9 个 L zip 合计仅 39 个结构、6/9 缺 rank_01；keep 目录只有 D-space 评估样本（不可顶替） | 打包时逐候选持久化 `lpeptide_keep_<task>/`，zip 期序列守卫回退；本轮已丢失的 L 结构无法恢复，需重跑 |
| 4 | **/status 完成信息指向不存在/过期的结构** | L 任务 rank1 行 structure_name 为空或指向已被清理的文件 | `pick_display_structure` 三级回退（rank1 → 首个存在结构的行 → 最低 rank 文件），`_archive_contains_structure`/`_archive_lowest_rank_structure` 落在纯函数模块 |
| 5 | **D_NFKBIZ 归档被并发双写污染**（>24h 排队重投递双跑的已知病灶在父任务级复现）：zip 内 JSON(00:28) 与结构(05:29) 来自两次上传，product 文件是 13.9 Å 解离构象、与 rank_01(2.87 Å) 不一致 | 条目时间戳 + md5 对比；其余 7 个 D 包 product 与 rank_01 逐字节一致 | product 条目重写为 rank_01 副本（原包留 .bak）；单跑锁补父任务级是后续项 |
| 6 | **DB 快照停滞**：17 行停留在中途快照（gen1、62-162 条累积行）、completed_at 全 NULL | 修复前后对照（rows_backup.json） | 以归档为源真理重写 17 行（confidence/structure_name/completed_at），SQL 与备份存档 |

**结构质量审查**（239 个结构逐一生信计算，gemmi）：D 系列 0 个 <2.2 Å 碰撞、手型全 D 正确；L 系列存活结构中 10/39 有真实重叠（最重 L_TLE rank_02 0.90 Å）；**D-TRAF6 rank_02 有 3 个残基真实翻回 L + 末端 CYS 体积 -13（畸变）**、rank_15 同类——导出手型门禁只查 rank1，已补逐 rank 遥测字段（不拒绝，<30% 评分策略不变）。

## 二、性能（网络 + CPU）——全部实测

| 改动 | 实测 |
|---|---|
| `/status/batch` 裁剪候选数组与重复键（单任务 /status 保持全量；前端轮询合并本就剥离候选行、候选来自结果包 hydration——消费方逐一验证） | 17 任务 ~2.6 KB/任务（-94%）；热批 10-14 ms（线程池约 5-7×） |
| `/results/<id>/view` 启用 ETag + conditional + max_age=60，**管理网关透传条件头/缓存头/304 无体**（审查发现直连生效但浏览器路径被网关剥头 + 前端 no-store 双重失效——P1） | 端到端 304 / 0 B / 7 ms（2.9 MB 归档重复打开场景） |
| 归档快照缓存（(path,mtime_ns,size) 键控，FIFO+竞态安全）+ chain_mean_plddt 快照级缓存 | 完成任务重复轮询为内存读；热路径仅剩 ~4-6 次 stat（审查员逐 IO 排序确认） |
| 304 计入 usage 成功（原 `succeeded = 2xx` 把缓存重验证记成失败、last_used_at 停更） | api_token_usage 语义修正 |

## 三、工程化 / 规范化

- **新模块**（均为纯函数/无重依赖，可直接单测）：`backend/runtime/peptide_archive_layout.py`（归档布局单一真源，11 测）、前端 `parserPrimitives.ts` + `peptideCandidateStorage.ts`（解析器与存储解耦，jszip 随 parser 变懒块）、`copilotSessionState.ts`（Copilot 同步态与 3.5k 行模态解耦）、`projectRuntimeRowHelpers.ts`（23 个纯函数从 2.4k 行 hook 拆出，7 行为锁测试）
- **懒加载**：ProjectDetailPage 632→336KB（-47%）、ProjectTasksPage 199→121KB（-39%）、resultBundleParser+jszip(~200KB) 成为独立按需块、Copilot 模态真懒载（everOpened 门控 + 页级 `vbio:open-copilot` 监听，见四-1）
- **测试**：后端 +12（layout 11 + cleanup keep 宽限 1）、前端 +11（源选择 4、top_results 回归 1、runtimeRowHelpers 7、residue 归一化 3——与既有重叠后净计）
- **视觉走查修复**：⌘K 打不开 Copilot 的回归（门控挡住唯一事件入口→页级监听）、**结果表 Score 列被前端固定权重（0.3 pLDDT+0.7 IPSAE）重算颠覆后端排名**（改为优先后端 composite_score，缺失才回退重算）、**逐残基 pLDDT 0-1 刻度未归一化导致气泡全橙**（`normalizeResiduePlddtSeries`）、"1 projects" 复数、两处搜索占位截断、登录占位文案、Suspense a11y
- **审查驱动修正的注释/文档失实**：`all_results` "已排序"的失实注释（代码实为不排序）、DESIGN_RANK_RE "keep in sync" 的夸大表述、缓存逐出注释的竞态路径遗漏（KeyError 变体由 reviewer 最小复现实证）

## 四、独立审查循环

- 三路并行（后端正确性/性能网络/前端 UX）首审均 PASS；交叉确认同一 P1（304 死代码）并新增发现（快照缓存 KeyError 竞态、cleanup 误扫 keep 目录、Score 重算错位由视觉走查溯源）
- 两轮回执复检收敛至最终 **PASS（无 P0/P1/P2）**；reviewer 实证手段含 16 线程 hammer（零错误）、逐函数字节比对、最小竞态复现脚本、端到端 304 复测
- knip 死代码扫描：仅 1 处未用接口导出，已清

## 五、遗留与建议（按优先序）

1. **路由级测试骨架缺失**：`/status/batch` 并行化、降级契约、`_compact`、快照缓存均为闭包内逻辑，仅靠 reviewer 一次性脚本实证。建议把 `register_task_routes` 的可拆逻辑继续外移为可 import 的纯函数层（本轮已完成一半：布局与选择逻辑已在 peptide_archive_layout）。
2. **L 系列结构补全**：9 个任务的结构已随 tmp 清理丢失（keep 目录只有 D-space 评估样本，语义不可顶替），需重跑；建议重跑前先部署本轮 worker 修复（lpeptide_keep 已在）。
3. **父任务级单跑锁**：候选任务已有 NX 锁（92e6c2b），D_NFKBIZ 的父任务双写污染证明父任务同样需要（或至少归档上传按 content-hash 去重）。
4. **pocket-constraint 8 例预存失败**：属并行会话工作区（docs/incremental-review-20261008.md 已记录），非本批引入。
5. **视觉可选**：失败任务红色加粗的视觉强度、S/F/O 徽标 hover 提示核对、结果区 4 个图标切换钮补文字 tooltip、SharingModal/首次 Copilot 的毫秒级 null-fallback 窗口。

## 六、验证快照

- 前端：tsc 0 错误；vitest 38 文件 / **319 用例全绿**；构建部署 dist-served；首屏五 chunk parser-free（grep 证据）
- 后端：pytest **250 passed + 1 skipped + 8 failed**（失败为 pocket-constraint 预存基线，逐例与 docs/incremental-review-20261008.md 吻合）
- 运行时：central API 已重启、管理 API 已 HUP；/status/batch、304、⌘K→Copilot、3D 卡片视图全部冒烟通过
- 视觉走查临时账号（visualqa）及其项目共享已删除


## 七、设计循环优化（GRPO 泛化 + 效率，两路独立审计）

数据基础：v11 批次 17 个归档的逐代 ipTM 曲线（L 平均 +0.09、D 平均 +0.05 且 4/8 平坦或退化）、序列多样性 100%（354/354 无重复）、checkpoint 分代完成数。

### 已实施修复

| # | 修复 | 根因 | 预期 |
|---|---|---|---|
| 1 | **proposal_group 携带断裂修复**：`generation_jobs.append` 补 `"proposal_group": candidate.get("proposal_group")`（原所有行 100% 落入 "ungrouped"） | 组键在派发时丢失 → GRPO 组相对优势退化为整批相对优势，去 novo 与 per-parent 子代混入同一基线 | GRPO 组内对比恢复设计意图 |
| 2 | **learn 改为每代整批一次**：原在 `_finalize_candidate` 内按候选完成逐个调用（部分队列），基线随完成顺序漂移、早完成行被重复训练 | `generation_done` 未数拒绝行，完成判定漏拒绝路径 | 消除完成顺序偏差与重复训练 |
| 3 | **拒绝候选注入 floor 负样本**：3 个拒绝路径的行补 `composite_score: 0.0`（learn 的 pLDDT 阻尼 ×0.1 使其不主导，但组内提供负对比） | 拒绝行无数值分 → learn 过滤掉 → 每代 25-40% 评估预算零学习信号（兑现 9764 注释承诺） | 拒绝不再白烧预算 |
| 4 | **D 精修 seed 确定化**：`(task, d, generation, sequence)` sha256 → 原 seed+resume 计数器使同一序列跨 checkpoint 续跑重评出不同分（奖励不可复现 = GRPO 噪声） | 奖励可复现是策略梯度的前提 | D 爬山信号去噪 |
| 5 | **pLDDT 门禁按手性分设**（L=55 / D=50，env 可覆盖）：D 精修复合物肽链 pLDDT 分布天然低于 L 共折叠，55 一刀切使 D 精英池塌缩到 4-7 行（D_bcCat 过门率 36-55%） | 精英池塌缩 → 父代打转 → bcCat/RBPJ 平坦 | D 精英池恢复多样性 |
| 6 | **D 精修快档 env 接口**：`PROTENIX2DOCK_REFINE_SAMPLING_STEPS` 透传 100 步快档（worker 已支持,循环未接） | 盲 D 路 200 步×8 样本全价；筛查场景可半价 | 筛查运行 refine 成本 -50% |

### 已确认结论（两路审计交叉）

- **L 侧学习为真但主要是选择驱动**：BindCraft2 式半贪心突变 + NSGA-2 精英保留在低噪 ipTM 地形上爬山；GRPO 通道存在但信号极弱（每 learn 新建 AdamW 单步 lr=1e-5、LoRA 只存内存、服务重启归零、L/D 梯度互写同一权重）
- **D 侧不学习的根因不是提议器**（D=坐标镜像,序列仍是 L 字母表,提议-奖励对应无错配），而是奖励管线层数：孤立预测→镜像 staging→best-of-8 固定-D 扩散→ipSAE,三层随机性淹没 1-3 残基突变效应
- **候选去重在 `len<population` 之前执行**：GRPO 模式坍缩时每代产出静默缩水,缩到 0 时循环静默 break（"提前终止"的代码机制）
- **fold 一失败 revoke 全波**：7 个任务 checkpoint 停在 gen1 即此模式（建议改单候选拒绝,未在本轮实施）

### 遗留（下轮候选）

- GPU 池被池外进程挤占（训练容器占 GPU2、ESM3 不走池常驻 GPU0 且 OOM）——ops 项,预期墙钟 2 倍
- 代边界 ~25 分钟串行段（propose 不与 refine 收集流水化）
- 拒绝改大额扣分替代拒绝（保预算又喂 GRPO 的变体）
- 跨靶点 adapter 持久化（new_adapter_dir 客户端一行 + 服务端按 chirality/靶点族加载）


## 八、界面指标统一调研（ipTM vs ipSAE，文献级）

问题：L 路线排序用 chain-pair ipTM（模型置信度派生）、D 路线用 ligand-ipSAE（PAE 派生），两路线数值不可比、GRPO 奖励语义分裂。是否统一、统一到哪边。

### 结论：统一到 ipSAE（置信度中高）

- 两侧路线均从 Protenix full_data 的 `token_pair_pae` 用**同一脚本、同一 cutoff（10Å）、同一肽侧 scoping、同一聚合**计算 ipSAE;chain_pair_ipTM 保留为免费监控指标继续落盘
- **不做绝对 ipSAE 阈值做接受/拒绝**（小肽 ipSAE 系统性偏低：d0~sqrt(n) 设计惩罚小界面，10-25 aa 肽的 0.13-0.59 区间即此）;RL 内只用组内相对优势，上线阈值需肽专用校准集重标
- 伴随指标：记录 ipSAE_d0dom（对界面尺寸不敏感变体）与 LIS

### 关键文献证据

1. **ipSAE 出处修正**：Dunbrack Jr. 单作者 preprint（bioRxiv 2025.02.10.637595，"Rēs ipSAE loquuntur"），非 Larsson。三点修正 ipTM：PAE cutoff 过滤、局部 d0、标量 PAE。基准：40 真异源二聚体+70 假配对，ipSAE 区分真/假互作优于 ipTM;明确覆盖 domain-peptide 场景;ipTM 缺陷实例 p53+MDM2：ipTM≈0.3 vs ipSAE≈0.75
2. **最大规模直接对决**（bioRxiv 2025.08.14.670059，3766 个实验表征 binder，15 靶点统一重折叠）：**AF3 ipSAE_min 为全数据最佳单一预测指标**，优于 ipAE(+1.4x average precision)与 ipTM——终点是体外结合，非 pose RMSD
3. **主流工具均为 PAE 系而非 ipTM 系**：BindCraft（i_pTM>0.5 + i_pAE<0.35 + peptide_filters 肽预设）、RFdiffusion（pAE_interaction<10Å）、AlphaProteo（min pAE interaction<1.5）——IPSAE/PAE 派生指标是事实标准
4. **GRPO 奖励适配**：(a) 两者皆 PAE 确定性函数无采样噪声，但 pair-ipTM 在干净两链体系饱和聚类（L 路线 ≈0.67 集中）→ 组内优势趋零;ipSAE 0.13-0.59 离散度反而是有效优势信号 (b) PAE 派生目标更适合迭代优化（BECraft pTMEnergy 弃 ipTM 因"梯度稀疏、不反映复合物似然"——RL 奖励同理） (c) ipSAE 预测器无关可跨引擎比较;pair-ipTM 数值不可跨引擎互通（Protenix 官方 issue #78 报告其偏高）
5. **小肽空白（共同）**：无文献给出小肽专用 ipSAE/ipTM 验证 cutoff;ipSAE 无公开 GRPO 先例——未开垦但机制上更优

### 实施路径（2026-10-11 已实施，见下）

**已落地**：设计器序列行内校验（非法字符行内红错，PROTEIN_REGEX 导出复用）；设计器 beforeunload 草稿守卫（new_task=1 时刷新/关闭弹窗）；失败任务 traceback 折叠（>220 字符或含 Traceback → 一行摘要 + view full log）；Copilot 副标题去统计泄漏；'1 projects' 单复数；搜索占位截断；Suspense a11y；reduced-motion 守卫；Molstar 启动占位；aria 批次。**未落地（下轮）**：preorganization 项、i_pDAE 排名、GRPO 奖励切 ipSAE 复合分（需 GPU 批次验证）。

### 实施路径（原计划，保留供对照）

1. L 路线候选收集路径接 `compute_ipsae_for_output`（L fold 的 full_data 已含 token_pair_pae，纯 numpy 计算已就绪）
2. 两路线 `interface_metric_source` 统一为 'ipsae'（UI IPSAE 列已存在;ipTM 列保留为监控）
3. composite 权重按 ipSAE 量纲重校（L 候选 composite 随之变化，排名顺序可能与旧版不同——预期内）
4. GRPO 奖励切到统一 ipSAE（组内相对优势自动吸收量纲差）