# V-Bio 肽设计平台 · 深度代码审查报告

- 日期：2026-10-08
- 审查人：会话内深度审查（本报告）；三路独立 agent 审查已于当日 18:27 由自动化任务 `automation-7bb491bc` 补跑（用量窗口重置后），其增量发现见附录引用
- 范围：`backend/`（runtime/worker/routes/monitoring）、`capabilities/peptide_lm`、`capabilities/protenix2dock`、`deploy/docker`
- 方法：AST 级异常处理清点、pyflakes 全量、grep 模式扫描（吞错/可变默认/shell=True/硬编码/TTL）、事故回放式路径走读、几何/统计实证（扫描脚本与证据见文末）

## 一、结论概览

| 级别 | 数量 | 已修复 | 状态 |
|---|---|---|---|
| P0（发运错误数据/必然复现） | 4 | 4 | ✅ 全部修复并有回归测试 |
| P1（确定性资源/一致性风险） | 3 | 3 | ✅ 全部修复 |
| P2（工程质量/漂移风险） | 8 | 1 | 其余列入债务清单 |
| P3（风格/冗余） | 5 | 0 | 记录在案，随拆分治理 |

本日相关提交：`0ee89b6` `e9a15e6` `9365313` `770a597` `180a65f` `3f83464` `ab9ce4a` `e9a15e6` `0edf56f` `a563240` `eb0d151`。

## 二、P0 —— 会发运错误数据（全部已修复）

| # | 发现 | 证据 | 修复 |
|---|---|---|---|
| P0-1 | **broker 必然重投递多天任务**：`task_acks_late=True` 全局 + `visibility_timeout=24h`，设计父任务运行 3-4 天 ⇒ 消息 100% 被重投，双进程竞跑曾产空心 zip/假 FAILURE/分离结构 | celery_app.py:46-51；事故：v11-D 四任务假 FAILURE、TRAF6/SOX2/beta-Cat/UHRF1 发运放置帧 | `predict_task` 改 `acks_late=False`（at-most-once）+ 检查点重提规程（9365313） |
| P0-2 | **导出回退发运未对接帧**：refined 丢失时静默回退 `d_space_staged`/keep 内 `staged.pdb`（肽在口袋表面、非对接位，且无 B 因子） | 实证 RMSD=0.000：TRAF6 rank2 ≡ flip(staged)；全 zip 扫描 B=0 指纹 | 移除 staged 回退；keep 回退必须序列匹配（9365313+770a597）；无法恢复的行诚实降级"无结构"+WARNING |
| P0-3 | **发运结构无结合态校验**：分离 74 Å 的"结果"曾直接入包且带高分 | 事故 zip 实测 0/24 bound | 新增 `_assert_product_bound`（binder-靶点重原子距 >12 Å 拒绝）；binder 链缺失时报错而非静默通过（回归测试抓出的洞） |
| P0-4 | **design_results.json 缺 structure_name**：前端 3D 卡片全部 No structure | e9a15e6 提交说明；前端 resultBundleParser.ts:1825 读该字段 | 导出时同步写回 all_results 行（e9a15e6） |

## 三、P1 —— 确定性风险（全部已修复）

| # | 发现 | 证据 | 修复 |
|---|---|---|---|
| P1-1 | **子任务注册表 TTL 24h < 父任务时长(数天)**：取消/终止父任务时映射已过期，在飞候选变孤儿烧 GPU（NFKBIZ gen-6 孤儿事故） | run_single_prediction.py:6111 `expire(key, 24*3600)`；cancel 路径 `_revoke_peptide_subtasks` 依赖该 key | TTL → 14d，对齐取消标志（eb0d151） |
| P1-2 | **检查点非原子写**：`write_text` 中途死进程 → JSON 截断 → resume 静默回退 gen 1（丢全部代） | 9137-9156 旧代码 | 临时文件 + `os.replace`（ab9ce4a） |
| P1-3 | **上传结构残缺末端残基毒化整个任务**：1HL3 THR332 仅 1 个 N 原子 → 模型按 L 重建 → D 手性门拒绝全部 144 候选 | d_target.pdb 实测 332 残基末位 1 原子 | staging 修剪首尾主链不完整残基（0ee89b6），离线+线上双重验证 |

## 四、P2 —— 工程质量与漂移风险

| # | 发现 | 证据 | 状态 |
|---|---|---|---|
| P2-1 | **staging 全不可用链占用链字母**：空链仍消耗 A/B/C 编号，下游链契约漂移 | 新增回归测试时发现 | ✅ 已修（a563240） |
| P2-2 | **12k 行单体** run_single_prediction.py：后端派发/设计循环/几何工具/导出/docker-spawn 混居；`import` 即拉起 gemmi/numpy 全家 | wc -l = 12035 | 📋 拆分方案见 engineering-standards.md §10.1；增量执行，入口兼容 |
| P2-3 | **常量散落**：12 Å 门禁、2.8 Å CA-CA、2.3/2.5 Å、PLDDT 55、SEZ 2.5 Å 等物理量内联多处 | grep 证据见扫描记录 | 📋 待建 design_constants.py（规范 §7） |
| P2-4 | **手性体积计算 4 份拷贝**（mirror.py×2、esm3_oracle.py:146、structure_quality.py:93）；**三字母表 5 份拷贝**（run_single_prediction、patch_checkpoint_structures、routes/lead_opt、protenix2dock/core/structure、peplm/data/mine_pdb_binders） | grep linalg.det / GLN 映射 | 📋 随 P2-3 收编 |
| P2-5 | **吞错清点**：核心文件 broad-`except Exception: pass` 共 18 处（runtime 17 + worker 1）。逐一 AST 定位核验：**全部为尽力而为语义**（进度回调、registry 清理、revoke、docker rm 预清理、提示语构造、代理排序）——本次未发现吞掉关键路径错误的实例，但缺统一注释约定 | AST 清点输出 | 📋 规范 §6 已定约定；新代码按此评审 |
| P2-6 | **检查点 resume 代数可越界**（gen 7 of 6）：range 语义使循环跳过、导出直进，行为正确但进度语义含混（completed 54/72 + 100%） | v14 运行记录 | 📋 低优；重构时收敛 |
| P2-7 | **GPU 等待集合无 TTL**：`sadd(GPU_WAITING_NON_PEPTIDE_SET_KEY)` 后进程死则成员残留，靠节流 reconcile 自愈 | gpu_manager.py:552 | 📋 观察；reconcile 存在（gpu_manager.py:315） |
| P2-8 | **网关旁路**：直连 central `/predict` 不产生 project_tasks 行（v12-v14 曾在前端不可见），需手工补插 | 本日运维记录 | 📋 长期：提交入口收敛到网关，或 central 落行 |

## 五、P3 —— 冗余/风格（随重构治理）

- `d_space_staged/d_space_refined/d_space_iptm` 等行字段与 `structure_source_path` 语义重叠，序列化时需四处 pop —— 拆分时统一为单一 structure 引用对象。
- 中英文错误消息混用 —— 规范 §6 已定约定，存量随触碰逐步收敛。
- `except Exception: pass` 18 处均需按 §6 补"为什么无害"注释（本次核验过语义，未改动行为）。
- 前端 `projectTaskSnapshot.ts` 白名单与后端 options 键名手工双维护（本次 v12 提交踩过：漏 `peptideStructureMode` 导致提案无 Cys）——建议前后端共享 schema。
- 后端任务名（监控 name 列）与前端显示名来自两处 —— v12+ 修复。

## 六、稳定性风险面（运维视角）

1. **投递语义矩阵**（规范 §1）——已落表；`peptide_candidate_worker_task` 保持 late-ack 的前提是单候选 <24h，建议后续加软超时 clamp（债务 #2）。
2. **磁盘**：keep 目录现 4.23 GB/2225 个、refine 目录 8465 个——均被 result_cleanup 90 天保留期覆盖（result_cleanup.py:161-176），自愈；检查点 31 个/7 MB 无忧。
3. **自动化任务卫生**：已删除过期的小时级监控自动化（引用已死 PID 与已完成批次，存在基于过期状态误动作的风险）；现存的 18:27 独立审查自动化确认在排程。
4. **迁移不可变**（9-22 校验和事故）与 **ESM3 常驻进程部署**（提案器旧代码服役 10 天）——均已写入规范 §3/§4 并提供脚本。

## 七、回归测试资产（事故 → 测试）

`backend/tests/test_peptide_design_export.py`（6 条，全绿）：
1. 分离结构拒绝（74 Å 合成样本） 2. 真实结合距通过（2.6/5/8 Å） 3. binder 链缺失必须报错 4. 链 B 序列读取 5. 1HL3 末端残基修剪 6. flip 保距。
既有 46 条 dpeptide 测试全绿（a563240 验证运行）。

## 八、证据脚本与数据

- 全 zip 扫描（互穿/B=0/分离）：`/tmp/scan_clash.py`、`/tmp/scan_dzips.py`（容器内可复跑）
- 放置帧实证：`/tmp/prove_staged.py`（RMSD=0.000）
- 检查点结构回填：`backend/scripts/patch_checkpoint_structures.py`（已入库）
- AST 吞错清点：本报告 §四 P2-5 的 python 片段

## 九、后续迭代顺序（建议）

1. 本周：P2-3 常量模块 + P2-5 吞错注释补齐（纯机械，低风险）
2. 下周：P2-1 单体拆分第一步（geometry utils + export 两块先出，入口兼容）
3. 月内：前端 options schema 与后端共享；候选软超时；网关旁路收敛
4. 持续：每次事故回填一条回归测试（规范 §8 模式），报告按月滚动
