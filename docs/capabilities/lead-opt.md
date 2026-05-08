# Lead Optimization / MMP

- 任务类型：`lead_opt`
- 接口：
  - `lead_optimization_mmp_query_task`
  - peptide design 编排父任务
- 运行节点：CPU 计算节点

## 环境变量

```env
CPU_WORKER_CAPABILITIES=lead_opt
CPU_MAX_CONCURRENT_TASKS=0
```

说明：
- `CPU_MAX_CONCURRENT_TASKS=0` 默认使用全部 CPU 核心。
- 留空 `CPU_WORKER_CAPABILITIES=` 时，默认启用全部 CPU 任务类型（当前包含 `lead_opt`）。

## 路由规则

调度到 `cap.lead_opt.default`（CPU 计算节点默认不监听 high priority）。
没有可用计算节点时返回 `503`。

## 安装说明

详见 [docs/deployment/capability-installation.md](../deployment/capability-installation.md) 的 `Lead Opt / MMP（CPU）` 小节。
