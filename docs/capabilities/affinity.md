# 功能：Affinity

- capability 名称：`affinity`
- 典型任务：`/api/affinity`、`/api/affinity_separate`
- worker 类型：GPU

## `.env` 配置

```env
GPU_WORKER_CAPABILITIES=affinity
```

## 路由规则

调度到 `cap.affinity.high` 或 `cap.affinity.default`。
无在线 worker 时返回 `503`，不会进入默认队列。
