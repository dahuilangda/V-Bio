# 独立审查增量报告（三路 agent）+ Copilot 产品级提升方案

- 日期：2026-10-08
- 方式：3 路独立只读审查 agent（任务生命周期 / 导出几何 / 工程质量）+ 3 路专项调研 agent（Copilot harness / 上下文与纠错 / 技能与环境），全部完成
- 本报告 = 六份报告的去重汇总 + 已实施修复 + 遗留路线图

## 一、代码审查 agent 的增量发现（相对此前自研审查的新增项）

### 已立即修复（commit 4c8d085）

| 级别 | 发现 | 影响 | 修复 |
|---|---|---|---|
| P1 | **12 Å 发运门禁被共价连接子中和**：binder 对"其余所有链"测距，SEZ 连接子与肽是共价键（~1.8 Å）⇒ 双环产品的门禁 min_d 恒 <2 Å，永不触发 | 分离的"双环产物"（连接子+肽整体漂离 60 Å）照发不误 | 新增 `_polymer_chain_roles` 按聚合链长度分角色（binder 8-40 残基 / target >40 / linker <8），门禁只测 binder↔target |
| P1 | **门禁 binder 链名取自 YAML 空间**：staging 会重字母化链名，多链靶点下陈旧链名测到的是受体-受体接触（假通过）或找不到链（假失败、整包崩溃） | 多链靶点（RANKL 三聚体等真实用例）门禁失效 | 角色分类自带 binder 自动探测（肽长度链），陈旧名字不再致命 |
| P1 | **binder_avg_plddt 读的是连接子链**：`sorted(chain_plddt)[-1]` 在 A(0.957)/B(0.878)/L(0.934) 中选中 'L'——全部双环候选的 pLDDT 门禁、composite binder 项喂的是连接子置信度 | GRPO/elite 训练信号失真（这就是 TRAF6 卡片 93.4 的来源） | `_chain_mean_plddt_from_structure` 排除 <8 残基短链 |
| P1 | **keep 回退序列校验硬编码链 B**：多链靶点重字母后永不匹配 → 回退通道整段失效（静默全降级） | 断点恢复场景多链靶点拿不回结构 | `_pdb_peptide_sequence` 自动探测肽长度的链 |
| P1 | **候选任务不查父取消标志**：已入队的候选在父被取消后照常启动烧卡 | 孤儿烧卡（payload 现带 parent_task_id，启动前查 task_cancelled 标志） | tasks.py 取消检查 + payload 加 parent_task_id |
| P2 | 检查点/注册表吞错无痕迹；`_auto_ss_profile`、`_normalize_peptide_gpu_ids` 死代码；`test_ccd_overlay_merge.py` 游离版本控制外 | 排障盲区 | 已改告警/删除/收编 |

### 记录在案的遗留（按修复优先序）

1. **候选墙钟无上界**（P1 候补）：GPU 等待默认无限 + 无 soft/hard_time_limit ⇒ 排队 >24h 仍可能被重投递双跑。三选一修复（time_limit / acks_late=False / redis NX 锁）——建议 redis 锁方案。
2. **revoke 均为 level-7**：分离会话的孙进程与候选容器不被程序化取消路径击杀（人工 monitor 兜底）。建议 `_revoke_registered_peptide_subtasks` 逐候选补 docker label 击杀。
3. **AF3 sitecustomize 模板损坏（P0 级，已修）**：commit 528e914（09-24"注释清理"）剥掉了模板内 8+ 处方法调用括号 → AF3 容器内的 release-date 注入/CCD 覆盖/ragged-A3M 补齐静默失效三周。已全部恢复括号并加文本断言防回归；同时修复活代码内同源的 `sample.lstrip.startswith` 调用。
4. pocket-constraint 测试 8 例失败为**预存问题**（干净 HEAD 同样失败，属并行会话工作区），非本次改动引入——已留给对应 owner。
5. 其余：e2e eval 与线上上下文形状漂移（RECENT_TURNS=8 vs 6+12）、p2d_task tmp 目录不清、双 worker acks 语义、`workflow_backend_values` 单源化——见 agent 原始报告（已归档）。

## 二、Copilot 现状判定（三路调研一致结论）

**底盘是产品级的**：观察账本与预算化上下文、反伪造三重审计（标识符/数值/机器状态）、错误分类学（rejected=参数错可自纠 vs unreachable=停机改天再试）、有界退避+Retry-After、逐页渐进式技能披露、任务行落地审计、确定性合成选择题。这些超过一般开源水平。

**三大产品级短板**（三路报告交叉确认）：

1. **控制面断裂**：steering/stop/follow-up 服务端机制完备但 UI 从未接通（`turn_key` 从未传）；用户 Stop 走裸 fetch abort；波内超时曾直接裸 SSE error 并丢弃整轮已付费数据。
2. **环境感知缺席**：worker 在线性/队列深度/任务状态这三个"平台现在能做什么"的活事实，copilot 完全看不到——只有静态目录 + 当前页面可见数据。后端 `/workers/capabilities` 现成，从未接线。还有后端枚举漂移（copilot 的 backend 枚举缺 `nesso`）。
3. **跨轮目标/观测零服务端状态**：目标 18 条消息后出窗（6+12 硬截断伪摘要）；`$fromObservation` 拒绝跨轮 id（检索 365 个候选后无法说"对比 #2 和 #7"）。

## 三、已实施修复（本批 commit）

**Copilot 服务端**（copilot.py）：
- 用户 Stop 走优雅终局：循环顶 abort 改 `last_issues=["aborted by user"]` + break，激活既有的友好停止文案分支（原为死代码），不再裸 SSE error。
- 读波中途 deadline/abort 转错误观测进主循环：由既有墙钟检查接手 pressure-exit salvage——已检索的数据不再被丢弃，前端不再见技术性报错。
- 模型网关 429/5xx 分级重试：2 次退避（Retry-After 感知，cap 8s）；4xx 立即失败走纠错指导。修掉"一次 503 杀死整轮 vs 连接抖动反而三次重试"的分层不一致。
- 新增 `platform.worker_status` 读技能：planner 提交前可读 worker/队列实况；描述内置"离线时如实告知并给替代方案，而不是提交进必定的 503"。

**平台侧**：
- 候选任务取消防线（见上表 P1 行）。
- 注册表/检查点吞错改告警（孤儿可排查）。
- 导出门禁链分类重构（F1/F2/F3/F4 四个 P1 一并修复）+ 死代码清理 + 游离测试收编。

**部署**：management-api 已带完整 .env 重启（5055 健在，auth 端点 400=正常校验），Copilot 修复即刻生效；后端 worker 的导出/取消修复随 4c8d085 在下次任务自然生效（候选进程每任务新起）。

## 四、下一步路线图（按产品影响排序）

**2026-10-08 更新**：#5 已实施（tasks.status 读技能 + RECORD_IDENTITY_FIELDS 加 taskId/taskRowId + 任务锚定记忆优先保留 + PLAN_CORRECTNESS 三条指引 + nesso 枚举单源化，commit 7e4eb3d）；**目标保持最小闭环已实施**（同 commit）：needs_input/await_confirmation 终局持久化 {goal_text, outline, plan_id} 到 project_copilot_states（state_key=copilot-goal:{session_id}，complete/failed 清除），客户端 buildCopilotConversationContext 无条件注入 active_goal，服务端 context_block 置顶 ACTIVE GOAL 行。剩余未实施：#1 控制面收尾、#2 观测缓存、#3 真流式、#4 CopilotGoal 完整对象（当前为最小闭环）、#6 评测产品化、#7 单体拆分、#8 债务。

1. **控制面收尾**（最高优先）：服务端签发 `turn_key` 控制帧 + 前端 `streamCopilotTurn` 传入 + Stop 按钮先 `requestCopilotStop`；steering 队列从进程内 dict 换 `project_copilot_states` 行（双 worker 安全）。
2. **跨轮目标对象 CopilotGoal**：needs_input 时持久 outline（不再丢弃），下轮装载为初始大纲——消除"回答一个选择题后整个计划重推"。
3. **观测缓存 + 跨轮 `$fromObservation`**（TTL 30min）：解锁"对比 #2 和 #7"型深度研究任务，绕开 20 条记录截断。
4. **LLM 真流式**：phase-2 回答 `stream:true` 增量帧，消除 60-170s 假死窗（产品体感最大单项）。
5. **`tasks.status` 读技能** + `RECORD_IDENTITY_FIELDS` 加 `taskId` + lifecycle 回执强制保留——"刚才那个任务跑完没"可查。
6. **评测产品化**：30-turn 回放套件（断言 ACTIVE GOAL 在场）、故障注入矩阵（400/503/timeout 恢复率）、eval 常量对齐线上形状。
7. **单体拆分**：按工程报告 §2.2 的模块表增量执行（amino_acids → geometry → msa_client → …），入口与测试门面契约已核实可保持。
8. **债务**：候选墙钟上界（redis NX 锁）、revoke 升级到容器击杀、p2d_task tmp 清理、sitecustomize 模板外置成可测文本模块。

## 五、验证

- 后端全量测试：238 passed（8 个 pocket-constraint 失败为并行工作区预存问题，干净 HEAD 复现，非本次改动）。
- 导出回归：9 条全绿（含 F1/F2 两条新回归测试——多链重字母靶点、共价连接子中和场景）。
- Copilot：gunicorn 带 .env 完整启动，模块导入即验证新技能注册与修复路径。
- 平台：14 容器健康，前端 200，central 403→正常鉴权。
