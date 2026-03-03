# 功能：Boltz2Score

- capability 名称：`boltz2score`
- 典型任务：`/api/boltz2score`
- worker 类型：GPU

## `.env` 配置

```env
GPU_WORKER_CAPABILITIES=boltz2score
```

## 路由规则

调度到 `cap.boltz2score.high` 或 `cap.boltz2score.default`。
无在线 worker时返回 `503`，不会进入默认队列。
