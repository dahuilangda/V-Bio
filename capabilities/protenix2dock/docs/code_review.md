# V-Bio Docking 链路 Code Review（前后端配合重点）

范围：`/api/boltz2score`（boltz2score + protenix2dock 双后端）从网关到结果展示的完整链路。

## 一、链路与契约总览

```
前端 SPA ──POST /vbio-api/api/boltz2score (multipart)
  └─ 网关 (vbio_management_api, :5055)   认证 + 快照落库 + 原样转发
       └─ 后端 (/api/boltz2score)         表单校验 → 按 backend= 分流
            ├─ boltz2score_task  → cap.boltz2score.*
            └─ protenix2dock_task → cap.protenix.* （_CAPABILITY_QUEUE_HOST 映射）
                 └─ docker run protenix2dock.py → output/…
                      └─ 归档 zip（protenix/output/ 前缀）
                           └─ upload_result_to_central_api → 前端下载解析
```

**前端解析契约**（resultBundleParser.ts 的硬约定）：
- 路径前缀 `protenix/output/` 才走 isProtenix 分支
- 按文件名 `_summary_confidence_sample_N.json` 枚举样本，`ranking_score` 选最优
- 同目录同名 `_sample_N.cif` 为对应结构 —— **一任务一最优结构**由此保证
- 界面指标读 `ligand_ipsae_max` → `ipsae_dom` → `iptm`（优先级递减）
  → IPSAE 字段必须并入 summary_confidence（core/ipsae.py 已做）

## 二、本次 review 发现并已修复的问题

| # | 严重度 | 位置 | 问题 | 修复 |
|---|---|---|---|---|
| 1 | **高** | protenix2dock_task.py 超时路径 | 引用未导入的 `SUBPROCESS_TIMEOUT` → 任务超时时抛 NameError 掩盖真实超时 | `_tasks.SUBPROCESS_TIMEOUT` |
| 2 | 中 | 归档 | `protenix2dock_summary.json` 被 walk + 显式各写一次 → zip 重复条目 | 删除显式写入 |
| 3 | 中 | final_meta | `gpu_id` 恒 None（`self.request.get` 取不到） | 记录 `reported_gpu` |
| 4 | 中 | affinity ckpt 挂载 | worker 容器内 `os.path.exists(host_path)` 必 False → ckpt 永不挂载 | 无条件挂载，训练容器侧报错 |
| 5 | 中 | ckpt_every | 路由/任务两层都没透传 → 周期检查点静默失效 | 两层补透传 |
| 6 | 低 | CLI | `ligand_chain_out="B"` 硬编码，多链蛋白 IPSAE 错链 | 提为 add_interface_metrics 参数 |
| 7 | 低 | CLI main() | 230 行单函数、内联 import、`np_save` 定义在调用后 | 重构为 parse_args/resolve_inputs/build_engine_inputs/add_interface_metrics 四段 |

## 三、遗留问题处理状态（第二轮）

1. ~~**表单字段静默丢弃**~~ → **已修**：route 显式收集 ignored_fields，响应 202 中返回 + WARNING 日志
2. ~~**docker 命令构造重复**~~ → **已修**：`backend/worker/docker_cmd.py` 公共骨架，protenix2dock/affinity_train 两任务接入（boltz2score 保持不动，见下）
3. ~~**stdout 未流式上报**~~ → **已修**：dock 任务逐行解析 `INFO protenix2dock:` 阶段行做心跳，硬超时改 deadline 制
4. ~~**IPSAE 权重硬编码**~~ → **已修**：`P2D_INTERFACE_WEIGHTS` env 覆盖
5. ~~**ckpt_every 透传**~~ → **已修**（路由+任务两层）
6. **前端快照 backend 值**：仍为表单原值（protenix），展示无逻辑分支，暂可接受
7. **ESM 缺席**：架构级改进，需引入 ESM 嵌入通道到 head 输入（训练管线改造），列入后续路线

## 四、本次同步应用的编码规范

- 删除自我指涉注释（"our fix" / "Nesso-style we added"），只留契约与非显然约束
- print("[Info]...") 杂烩 → logging 统一格式（可解析、可静默）
- main() 按 pipeline 阶段拆函数，单一职责，输入输出显式
- 函数定义先于使用；魔法数字（权重、默认链名）提为模块常量并注释来源

## 五、第三轮：深度工程化 review（重点：训练/推理同路径，去重复实现）

本轮标准：代码必须是"真正的工程化"——同一计算只有一份实现、无死代码、无共享可变状态、
并发安全、资源确定性释放。GPU 被 4 卡训练占用，本轮验证用 CPU 功能测试 + 真实命令构建。

### 5.1 本轮发现并已修复

| # | 严重度 | 位置 | 问题 | 修复 |
|---|---|---|---|---|
| 1 | **高** | train_affinity.py `_grad_entry` | 用 ~45 行**完整复制** head 前向（z_linear/s_to_z/index_add_/blocks/掩码池化/双头），与 modules/affinity.py 必然漂移——训练与推理对不上时无任何报错 | head 加 `return_tensors=True`：`_grad_entry` 改为 8 行包装，调用 `head.forward(...)` 走唯一代码路径；梯度经 `affinity_pred_value_t` 保留 |
| 2 | **高** | affinity.py `_forward_single` | `B>1` 分支用 `z.unbind(1)`（错维），且 `x_pred` 循环外已按样本展开——B 恒为 1，该分支既错且死 | 删除分支，`assert B == 1` + 调用方 keep single-structure 语义 |
| 3 | 中 | affinity.py `forward` | `pos_lt/pos_rt` searchsorted 计算后从未使用；`_interface_min_distances` 方法无调用者 | 删除死代码 |
| 4 | 中 | affinity.py | `self._precompute` 实例共享可变状态，跨 forward/_forward_single 传参；训练侧也写它——并发/嵌套调用即错 | 改为显式参数传递 dmin/lt_u/rt_u，模块无残余状态 |
| 5 | 中 | 语义回归风险 | 重构时可能丢"crystal-pose 优先"（structured 样本用真实坐标、无 pose 才用 distogram expected_dist）——训练与 head 行为不一致 | `_grad_entry` 显式 `expected_dist=None if coords is not None else expected_dist`，维持 pose 监督优先级 |
| 6 | 低 | ipsae.py | `lig_atoms` 变量计算后未用（真用 `lig_mask`） | 删除 |
| 7 | 低 | structure.py | `_TO_STANDARD` 重复键 `"MLY": "K"` | 去重 |
| 8 | 低 | runner.py | `_first()` 递归仅自引用、无调用者 | 删除 |
| 9 | 低 | input_prep.py | MSA 并发写：4 训练容器同序列竞争写同一 `xxx.a3m` → 半截文件；另 `shutil` 在原子写改造后残留 import | `_atomic_write`（tmp + os.replace）；顶部 import 清理 |
| 10 | 中 | affinity_train_task.py | `shard_csv` 追加第二个 `--index_csv`（靠 argparse last-wins 碰巧生效） | 一次决定：在列表内替换旧值 |
| 11 | 中 | train_affinity.py | 默认 `val_csv` 缺失时 `with open` 直接崩溃（训练前挂） | 缺失→warning + 跳过 val gate |
| 12 | 低 | train_affinity.py | `samples` 用 lambda-`open()` 泄漏文件句柄 | with-open，与 val_rows 一致 |
| 13 | 低 | protenix2dock_task.py | extra docker args 未走 `_sanitize_docker_extra_args`（与 boltz2score 成熟模式不一致） | 复用 sanitize |

### 5.2 已评审确认无需改（本轮边界内）

- `modes.py` 五模式 sigma/steps/anchor 与 boltz2score MODE_CONFIGS 对齐，注释给出映射依据
- `runner.py` `build_configs` 双 pass 合并（base + model_type）与 runner/inference.py::main 一致
- `protenix.py` 集成点：affinity head 在 `_main_inference_loop` 内、confidence head 之后读 trunk 张量，
  结果并入 summary_confidence（stock dumper 零改动）；多 ckpt 集成按值平均 + 跨头 std
- 心跳契约：CLI `logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")` 输出 `INFO protenix2dock: …`
  ↔ 任务侧 `_STAGE_RE` 正则精确匹配；训练侧 `[progress] epoch=…` ↔ `_PROGRESS_RE`
- `docker_cmd.py` 共享骨架被 dock/训练两任务接入（第三处调用者即可验证非重复）

### 5.3 验证方式（GPU 被占，未跑端到端）

- CPU 功能测试：head 三路径（x_pred / expected_dist / return_tensors 梯度）数值与梯度正确；
  3-sample 推理、无 `_precompute` 泄漏
- 真实 import 构建两种任务 docker 命令：nvidia runtime / gpus / ro 挂载 / shm / script / 参数全部正确
- 进度正则与训练输出格式精确匹配；AST 全文件通过

### 5.4 剩余注意（下一轮）

1. **可选训练超参面覆盖**：scheduler/warmup 暂为 AdamW 固定 lr，无 cosine decay——对标 boltz2 训练可用 cosine 尾段提泛化
2. **c_s 断言**：head 用 `s_inputs.shape[-1]` 动态定 c_s，若 trunk(protenix-v2) 嵌入宽变化需重训而非静默错配（当前已有 checkpoint config 锁维，resume 已按 config 重建 head）
3. **boltz2score 任务**未接入 docker_cmd（保持独立实现）——若未来需要统一销毁逻辑再收拢

## 六、第四轮：OOM 根治 —— 复用 Protenix 原生 pairformer 机制（非自研）

### 6.1 训练 OOM 根因（4 卡于 ~330min 全部崩溃）

`ProtenixAffinityHead._InterfacePairBlock` 用 `nn.MultiheadAttention(key_padding_mask=…)`：
慢路径把 `[B*N, heads, N, N]` 注意力权重整体物化（fp32），N=999 时单层 ≈16GB、16 层直接打爆
24GB 卡。`del`/手写 chunk 无法解决：autograd 会保留每个 chunk 的中间张量；
bf16+SDPA 在传入非 contiguous q/k/v 时会静默回退 math 后端再次物化——全是补丁，已废弃。

**真正工程解（Protenix 自己解决 N=2150 的方式）**：
- `TriangleAttention`（StartingNode/EndingNode = row/col pair attention，语义与旧 block 同构）
  其 queries 经 `protenix.model.utils.chunk_layer` 分块，每块只物化 `chunk*heads*N^2`
- `_chunked_transition`：Transition 4x 中间同样分块
- `checkpoint_blocks(blocks_per_ckpt=1)`：backward 重算 block 前向，峰值由单 block 限定
- 动态 chunk_size：N>640 时 chunk=32 + checkpoint；小结构走原路径

### 6.2 本轮修复清单

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| 1 | affinity.py block | 手写 `_ChunkedMHA`（bf16 SDPA + contiguous 补丁）是死路 | 整个删除，改用 `TriangleAttention` + `Transition` + `chunk_layer` + `checkpoint_blocks` |
| 2 | block.forward | mask 传 bool 给 TriAtt（内部 `inf*(mask-1)` 运算需要 float） | 入内 `.float()` |
| 3 | `_interface_pair_mask` | interface mask 无对角 → 受体行整行 -inf → softmax NaN（旧代码有 dead-row 显式清零，TriAtt 没有） | `pair \| torch.eye(…)`，与 Protenix 自带 pair mask 一致 |
| 4 | `_forward_single_impl` | `checkpoint_blocks(args=(z, pm))` 语义错：它把每个 block 的**输出**当下一 block 输入，pm 会丢 | 常量全部 `partial(block, pair_mask=…, chunk_size=…)` 绑定，args 只传 `(z,)`（同 PairformerBlock._prep_blocks） |
| 5 | head.__init__ | `_InterfacePairBlock(c_z, num_heads, dropout)` 第三参数新签名是 `c_hidden_pair_att` → 错位 | 改关键字调用 |
| 6 | block.__init__ | 残留引用未删除的 `DropoutRowwise` | 删除（dropout 由 `dropout_add_rowwise` 处理） |
| 7 | train_affinity resume | 旧 ckpt（step 400-800，旧 MHA 结构）resume 会以晦涩 key 错误失败 | load_state_dict 包 try，架构不匹配时给出明确错误说明 |

### 6.3 验证（CPU-only，GPU 被其他任务占满 —— 遵守用户指示未开 GPU）

容器内 CPU 回归全绿：x_pred/expected_dist 两推理路径、train fwd+bwd 梯度（64/72 参数）、
dead-row 无 NaN、N=700 chunked+checkpoint 训练路径。AST/invariants 全过。

### 6.4 后果与决策

- **旧 4 个 big6 ckpt 作废**：架构升级是 breaking change（state_dict 键全变），
  不写手工键映射（映射 MHA→TriAtt 的 qkv/gating 不可靠，属于 toy）。重训即可——
  数据/切分/超参均不动，纯 head 结构换为 Protenix 原生件。
- **编排器 post_bighead.sh 已跑完但基于旧 ckpt**：ensemble_eval/pose_probe 输出里
  affinity_pred_value 全部缺失（新代码加载旧 ckpt 失败被静默吞掉），结果作废，
  待新 ckpt 出炉后重跑集成评估。
- 待 GPU 空闲后：GPU 实测 N=999 训练峰值 → 重训 4 shard → ensemble eval。
