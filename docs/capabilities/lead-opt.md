# 功能：Lead Optimization / MMP

- capability 名称：`lead_opt`
- 典型任务：
  - `lead_optimization_mmp_query_task`
  - peptide design 编排父任务
- worker 类型：CPU

## `.env` 配置

```env
CPU_WORKER_CAPABILITIES=lead_opt
CPU_MAX_CONCURRENT_TASKS=0
```

说明：
- `CPU_MAX_CONCURRENT_TASKS=0` 默认使用全部 CPU 核心。
- 留空 `CPU_WORKER_CAPABILITIES=` 时，也会默认启用全部 CPU 功能（当前包含 `lead_opt`）。

## 路由规则

调度到 `cap.lead_opt.default`（CPU worker 默认不监听 high priority）。
无在线 worker 时返回 `503`。

## 安装说明

详见 [docs/deployment/capability-installation.md](../deployment/capability-installation.md) 的 `Lead Opt / MMP（CPU）` 小节。
