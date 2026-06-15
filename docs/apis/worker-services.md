# Worker 服务状态 API

## 接口

- `GET /workers/capabilities`
- `GET /workers/cluster_status`（别名）

都需要 `X-API-Token`。

## 返回字段

- `summary`：集群汇总
  - `workers_total`
  - `capabilities_total`
  - `capabilities_online`
  - `slots_total / slots_busy / slots_idle`
  - `gpu_slots_total / cpu_slots_total`
- `workers`：单台 worker
  - `worker_type`（gpu/cpu/mixed）
  - `capabilities`、`capability_count`
  - `resources`（槽位总数、忙闲、GPU/CPU 槽位）
  - `tasks.active/reserved/scheduled`
  - `task_counters.executed_total_since_start`
- `capabilities`：按服务名聚合
  - `worker_count`
  - `max_running_tasks_upper_bound`
  - `gpu_slots_total / cpu_slots_total`
  - `active_tasks_count / reserved_tasks_count / scheduled_tasks_count`

`capabilities` 是接口字段名，对应平台里的模型服务。

## 用途

- 展示每台服务器提供哪些模型服务。
- 查看最大并发、当前占用和排队情况。
- 判断某个模型服务是否没有可用 worker。
