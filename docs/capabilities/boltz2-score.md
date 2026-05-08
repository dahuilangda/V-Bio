# Boltz2Score

- 任务类型：`boltz2score`
- 接口：`/api/boltz2score`
- 运行节点：GPU 计算节点

## 环境变量

```env
GPU_WORKER_CAPABILITIES=boltz2score
```

## 路由规则

调度到 `cap.boltz2score.high` 或 `cap.boltz2score.default`。
没有可用计算节点时返回 `503`，不会进入默认队列。
