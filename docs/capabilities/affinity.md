# Affinity

- 任务类型：`affinity`
- 接口：`/api/affinity`、`/api/affinity_separate`
- 运行节点：GPU 计算节点

## 环境变量

```env
GPU_WORKER_CAPABILITIES=affinity
```

## 路由规则

调度到 `cap.affinity.high` 或 `cap.affinity.default`。
没有可用计算节点时返回 `503`，不会进入默认队列。
