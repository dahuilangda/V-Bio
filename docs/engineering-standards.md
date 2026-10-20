# V-Bio 后端工程规范

> 2026-10-08 制定。（重投递双跑 → 空心 zip / 假 FAILURE / 分离结构发运 / 前端无结构卡片）。本文档把每次事故的教训固化成可执行的约束。**新增代码违反本文档视为 bug。**

## 1. 任务投递语义（最高优先级）

| 规则 | 原因 |
|---|---|
| 运行时间可能超过 `visibility_timeout`(24h) 的任务**必须** `acks_late=False`（at-most-once）。目前 `predict_task` 已改；`peptide_candidate_worker_task` 保留 late-ack（单候选 <24h，丢失可由父任务感知） | 多天父任务 + late-ack = 必然重投递；重复进程会竞跑、覆盖结果、污染状态 |
| 任何任务**不允许依赖重投递来容错**。崩溃恢复走两条路：① 设计父任务 → 代级检查点 + 人工/脚本重提（移植 checkpoint JSON）；② 短任务 → `autoretry_for`（仅网络类瞬时错误） | broker 重投递 ≠ 崩溃恢复；它是重复执行 |
| 新增长任务（预计 >12h）必须在 PR 里声明投递语义并对照本表 | 防止下一个 predict_task |

## 2. 导出契约（fail loud，禁止静默降级）

1. **禁止用替代数据冒充结果**：refined 结构丢失时，禁止回退 staged 帧（那是未对接的几何，发运=欺诈性结果）。宁可：该行无结构 + WARNING + UI 显示"无结构"。
2. **发运门禁**：每个入 zip 的产品结构必须过 `_assert_product_bound`（binder-靶点重原子距 ≤12 Å）。几何上不可能的结果（悬空肽）不允许带高分出现在用户面前。
3. **分数与结构必须同源**：`design_results.json` 与 `results_summary.json` 由同一份行数据写出；任何新字段（如 `structure_name`）必须同时写两处。前端消费的是 design_results——评审 PR 时先问"这个字段前端从哪个文件读"。
4. **留存目录（keep）必须带代数**（`dpeptide_keep_<task>_genXXX_candYYY`），且按序列校验后才能作为恢复源。候选序号每代重复，不带代数的 key 必然跨代覆盖。

## 3. 长驻进程部署清单

| 组件 | 重载代码的方式 | 检查点 |
|---|---|---|
| celery workers（docker） | `bash backend/run.sh restart <targets>` | — |
| **ESM3 HTTP 服务（宿主机常驻）** | `bash deploy/restart_esm3_service.sh` | 改 `peplm/integrate/**` 后**必须**跑，否则旧策略静默服役 |
| GPU worker 派生的候选容器 | 每次任务新起容器，天然新代码 | — |
| 前端 | `bash frontend/run.sh restart` | dist-served 累积式，旧 tab 不坏 |

**改完代码 ≠ 部署完成**。提交前对照上表；涉及提桔回路（ESM3）的改动在 commit message 里注明"需重启 ESM3"。

## 4. 数据库迁移

- 迁移文件**一经应用不可修改**（monitor 启动校验 sha256，不匹配 → 崩溃循环）。修改 = 新增一个迁移文件。
- 恢复 procedure：手工补跑幂等 SQL + `UPDATE vbio_schema_migrations SET checksum=...`（2026-10-08 发生过一次）。

## 5. 网络与配置

- **禁止硬编码宿主机 IP**。容器访问宿主机服务一律 `host.docker.internal`（compose `extra_hosts: host.docker.internal:host-gateway`；派生容器经 `PROTENIX_DOCKER_EXTRA_ARGS` 传 `--add-host`）。
- 例外：`FRONTEND_URL` 指向物理 IP/LAN 域名是有意的（邮件里的链接要用户浏览器可达）。
- env 文件含 token，不进 git；改 env 后必须 `up -d`（`restart` 不会重读 env）。
- 新增 compose 服务时核对三件套：`extra_hosts`、`env_file`、网络可达性。

## 6. 错误处理约定

- **静默吞错仅允许两种写法**，且必须带注释说明为什么无害：
  ```python
  except OSError:
      pass  # checkpoint is best-effort; run continues without it
  ```
  其余一律记日志或抛出。全库审计入口：`grep -rn "except.*:\s*$" -A1 --include=*.py backend/`。
- 错误消息：面向用户的运维错误用中文（与平台 UI 一致）；内部不变量断言用英文。格式：`<子系统> <什么坏了> (<关键上下文: 路径/任务ID/数值>)`。
- `RuntimeError` = 运行环境/上游数据问题；`ValueError` = 调用方参数问题。不自创异常类之前先问是否真的需要。

## 7. 几何/数值常量单点定义

所有魔法数集中到 `backend/runtime/design_constants.py`（新建），带物理含义注释：

```python
PRODUCT_BOUND_MAX_DISTANCE_A = 12.0   # 发运门禁：binder-靶点最小重原子距
BACKBONE_CA_CA_MIN_A = 2.80           # 主链完整性
REFINE_INTERPENETRATION_A = 2.3       # 界面互穿阈值
REFINE_PACKING_BAND_A = 2.8           # packing-band 计数阈值
DPEPTIDE_PLDDT_GATE_DEFAULT = 55.0
SEZ_RING_BOND_CUTOFF_A = 2.5          # SEZ 三锚点成键判定
```

规则：同一个物理量在代码里出现第二次时必须收编。注释里写**来源**（晶体参照/事故链接）。

## 8. 必备测试（事故回归集）

`backend/tests/test_peptide_design_export.py`（新建），每条对应一次真实事故：

1. `test_export_row_carries_structure_name` — 导出后 design_results.json 每行有 structure_name 且文件存在（事故：前端全部 No structure）。
2. `test_export_never_falls_back_to_staged_frame` — refined 缺失 + keep 缺失 → 行无结构 + WARNING，断言 zip 内不存在来自 staged 帧的 rank 文件（事故：74 Å 悬空肽）。
3. `test_assert_product_bound_rejects_dissociated` — 构造 74 Å 结构 → RuntimeError；2.6 Å 结构 → 通过。
4. `test_keep_fallback_requires_sequence_match` — keep 的 staged 肽序列与行不一致 → 不采用（事故：跨代覆盖风险）。
5. `test_d_target_trim_drops_incomplete_terminal_residues` — 1HL3（THR332 仅 1 个 N）→ staged 链尾为 LEU331、pocket 编号不受影响。
6. `test_checkpoint_resume_caps_generation` — checkpoint.generation=6, iterations=6 → _start_generation 收敛到 ≤iterations，不越界。
7. `test_peptide_archive_layout.py`（2026-10-10）— 展示结构三级回退（rank1 → 首个存在结构的行 → 最低 rank 文件）；事故：断点续跑后 /status 指向已清理的 rank_01.pdb，用户看到"无结构"。
8. `peptidePreferredArchive.test.ts`（前端，2026-10-10）— canonical top_results 优先于 design_results 完成序 dump；事故：任务卡分数冻结在 gen1 首个完成候选（0.13-0.44 vs 真实 0.59-0.97）。
9. `projectRuntimeRowHelpers.test.ts`（前端，2026-10-10）— 运行时合并不回退终端态；事故：已完成任务被陈旧载荷拉回 PENDING。
10. `parseHelpers.residue.test.ts`（前端，2026-10-10）— 逐残基 pLDDT 0-1/0-100 双刻度归一化；事故：高置信候选的氨基酸气泡全橙。

## 9. 评审检查单（PR 自查 + reviewer 必查）

- [ ] 新任务 >12h？→ 投递语义声明（§1）
- [ ] 新增字段？→ results_summary 与 design_results 双写（§2.3）
- [ ] 捕获异常？→ 符合 §6 两种模式之一
- [ ] 新魔法数？→ 进 constants 模块（§7）
- [ ] 改了 peplm/提案器？→ ESM3 重启提醒（§3）
- [ ] 新容器/compose？→ extra_hosts + 无硬编码 IP（§5）
- [ ] 改了迁移？→ 必须是新文件（§4）
- [ ] 几何路径改动？→ 对照 §8 回归测试跑过

## 10. 已知债务（按优先级）

1. `run_single_prediction.py` ~12k 行单体：按 backend/worker、design loop、geometry、export、docker-spawn 拆模块（入口 `python -m backend.runtime.run_single_prediction` 保持兼容）。拆分期间禁止顺手改逻辑。
2. `peptide_candidate_worker_task` 的 acks_late=True 依赖"候选 <24h"假设——候选超时上限应显式 clamp（软超时 kills 长尾）。
3. checkpoint JSON 非原子写（`write_text`）：进程死在写入中 → JSON 截断 → resume 失败回退 gen1。改为 temp+`os.replace`。
4. 监控表 `monitor_tasks_current` 对 acks_late=False 任务被杀后停留 RUNNING（靠 lease 过期），UI 需要显示 STALE 而非 RUNNING。
5. 父任务级单跑锁缺位（2026-10-10 实证）：候选任务已有 redis NX 锁，但父设计任务被 >24h 重投递后仍可双跑——D_NFKBIZ 归档曾被两次上传互相污染（product 与 rank_01 来自不同构建）。建议沿用候选锁机制扩展到父任务，或归档上传按 content-hash 去重。
6. `/status/batch` 的并行化、`_compact` 裁剪与归档快照缓存仍是 `register_task_routes` 闭包内逻辑，无路由级测试骨架；建议将并行化、裁剪与缓存拆出为独立路由模块并补充路由级测试。
7. L 路线断点续跑曾丢失全部候选结构（tmp 清理 + 无持久回退）——lpeptide_keep 回退已补（2026-10-10），但已丢失的 v11-L 批次结构需重跑补齐。
