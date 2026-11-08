# 任务状态更新与负载：现状、业界方案与演进设计

适用范围：任务列表/详情页的任务状态刷新链路（前端轮询 → 管理 API 网关 → 中心 API `/status/batch`）。

## 1. 现状链路（已落地的优化一并记录）

```
worker 状态 → Celery result backend (Redis) / tracker (Redis) / 结果归档 (磁盘)
                     │
浏览器 ──2.5s/4s 自适应轮询──► 管理 API 网关 ──► 中心 API POST /status/batch
   ▲                              │                │ 16 线程池，逐任务：
   └── 行合并（未变行保留引用）◄──┘                │  · find_result_archive（5s TTL 缓存 + glob 兜底）
                                                   │  · AsyncResult → Redis
                                                   │  · tracker/心跳 Redis 读
                                                   └─ 每任务 I/O ≈ 数 ms，408 任务/批 ≈ 数百 ms
```

已就位的优化：自适应调度（单飞、抖动、隐藏页 ×2、退避、30s 上限）、批量端点（2000 上限、
逐任务 UNKNOWN 降级）、payload 压缩、轮询未变行保留引用（前端 memo 链）、签名 WeakMap 缓存、
后台批 256 的轮转游标。

**当前负载模型**（单客户端、408 queued 项目）：4s 一 tick，每 tick 一次批量 POST，
服务端 176 次每任务 I/O + ~41KB×N 响应体；多客户端不共享读取。

## 2. 业界参考

| 系统 | 状态更新方式 | 要点 |
| --- | --- | --- |
| Kubernetes | LIST 一次 + WATCH 增量流（`resourceVersion` 令牌） | 令牌保证不丢事件；版本被回收返回 410 → 客户端重 LIST。这是"列表+增量"的权威设计 |
| Airflow / Temporal UI | 纯客户端轮询（auto-refresh / refetch interval），无服务端推送 | 轮询对工作流 UI 是业界可接受的基线；推送由用户自建（Redis pub/sub → 中转层） |
| HTTP 条件请求 | `ETag` + `If-None-Match` → `304` 空体 | 省响应体与前端解析；注意 CDN 对弱校验器的处理 |
| SSE 生产化 | nginx `proxy_buffering off` + `read_timeout 24h` + 心跳；gthread 一连接一线程 → 大规模需 gevent/uvicorn；多 worker 需共享事件总线 | 本仓管理 API 为 gthread 2×8，内网小并发可承受 |
| Supabase Realtime 自托管 | Postgres 逻辑复制（Elixir 服务） | 功能强但多一个有状态服务；更轻的替代是 `LISTEN/NOTIFY` + 小型扇出 |

结论：**"列表 + 版本化增量 + 可选推送"是标准演进路径**；纯轮询并非落后方案（Airflow/Temporal 即如此），
缺的是**版本令牌**与**HTTP 缓存语义**。

## 3. 差距分析

| 缺口 | 现状 | 后果 |
| --- | --- | --- |
| 无版本令牌 | 每 tick 全量 200 | 无法区分"无变化"，带宽/解析/重渲染全量发生 |
| 无 HTTP 缓存语义 | 无 ETag/304 | 多客户端读取不共享 |
| 每任务 I/O 无跨请求共享 | archive 5s TTL 仅限单任务单进程内；两个客户端同 tick 重复读 | 客户端数 × 轮询频率 线性放大 |
| 推送通道缺失 | 任务页无 SSE（管理页 monitor SSE 基建已有：PG LISTEN/NOTIFY + broker + 合并 + 心跳） | 状态可见延迟下限 = 轮询间隔 |

## 4. 分阶段设计

### 阶段 1：HTTP 缓存语义 + 跨请求快照（约 1 天，纯后端 + 前端 header）

- `/status/batch` 计算规范化负载哈希（`task_id|state|updated_at|status_text`）→ `ETag`；
  请求带 `If-None-Match` 且匹配 → `304` 空体。
- 模块级快照缓存：`task_id → (etag, payload)`，TTL 2.5s（与最快轮询同周期）。
  同项目多标签页/多用户在 TTL 内共享一次每任务 I/O；命中同 ETag 直接 304。
- 前端 `getTaskStatuses` 保存上次 ETag，请求附 `If-None-Match`；304 时跳过合并与渲染。

### 阶段 2：版本化增量端点（约 2-3 天）

- 状态版本源：worker/调度侧状态变更处 `INCR vbio:status:epoch`（全局单调）。
- 新端点 `GET /tasks/status/delta?since=<epoch>&project=<id>`：返回
  `{epoch, changed: [{task_id, state, status_text, updated_at}]}`；无变化返回 `204`。
- 语义对齐 k8s watch：epoch 即 `resourceVersion`；前端持有 epoch 轮询 delta（间隔可放宽至 5-8s
  而不损失及时性），epoch 过期（后端保留窗口）返回 `410` → 客户端重拉一次全量 batch 对账。
- `/status/batch` 保留为初始 LIST 与对账路径。

### 阶段 3：项目级推送通道（可选，约 3-5 天）

- 复用管理 API 已有的 monitor SSE 基建（`monitor_stream.py`：PG `LISTEN/NOTIFY` +
  broker 订阅 + 合并 + 心跳 + SSE 生成器），新增任务通道：
  状态落库触发 `NOTIFY vbio_task_events`（worker 侧或中心 API 侧）→ SSE fan-out。
- 前端 `EventSource` 订阅变更事件 → 触发一次 delta 拉取；轮询降级为对账兜底（30-60s）。
- 运维前提（生产化清单）：nginx `proxy_buffering off` / `read_timeout` / HTTP1.1；
  gthread 下每连接占一线程 —— 内网小并发可承受，公网大并发需 gevent/uvicorn 或独立轻量网关。

### 明确不做

- Supabase Realtime 自托管：多一个 Elixir 有状态服务 + 逻辑复制，运维成本大于收益；
  本仓已有 LISTEN/NOTIFY + SSE 等价基建。
- WebSocket：单向状态推送用不到双向能力；SSE 语义更简单、重连原生。

## 5. 负载演进预估（408 任务项目、单客户端）

| 阶段 | 每 tick 服务端 I/O | 响应体 | 状态可见延迟 |
| --- | --- | --- | --- |
| 现状 | 176 次每任务读 + 41KB×N | 全量 | ≤4s |
| 阶段 1 | 不变（304 时省 jsonify 与传输） | 304 空体 | ≤4s |
| 阶段 2 | 仅变更任务读（通常 0-数十次） | 数行 | ≤5-8s（间隔可放宽） |
| 阶段 3 | 变更即推，无轮询 | 事件帧 | 秒级 |

多客户端线性放大问题在阶段 1 的快照共享与阶段 2 的 epoch 缓存下被消除。
