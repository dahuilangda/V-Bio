# Worker 功能与集群状态 API

## 接口

- `GET /workers/capabilities`
- `GET /workers/cluster_status`（别名）

都需要 `X-API-Token`。

## 主要字段

- `summary`：全集群汇总
  - `workers_total`
  - `capabilities_total`
  - `capabilities_online`
  - `slots_total / slots_busy / slots_idle`
  - `gpu_slots_total / cpu_slots_total`
- `workers`：每台 worker 详情
  - `worker_type`（gpu/cpu/mixed）
  - `capabilities`、`capability_count`
  - `resources`（槽位总数、忙闲、GPU/CPU 槽位）
  - `tasks.active/reserved/scheduled`
  - `task_counters.executed_total_since_start`
- `capabilities`：按功能聚合
  - `worker_count`
  - `max_running_tasks_upper_bound`
  - `gpu_slots_total / cpu_slots_total`
  - `active_tasks_count / reserved_tasks_count / scheduled_tasks_count`

## 用途

- 给前端展示“哪台服务器有什么功能、最大并发多少、当前占用多少”。
- 运维定位某功能是否无 worker、是否过载、是否积压。
