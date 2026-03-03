# 后端工程结构

## 目录结构

```text
backend/
  app.py                  # Flask 入口与路由装配
  core/                   # 核心运行时配置与 Celery app
    config.py
    celery_app.py
  worker/                 # Celery 任务实现
    tasks.py
  runtime/                # 预测/适配执行逻辑
    run_single_prediction.py
    run_affinity_prediction.py
    af3_adapter.py
    protenix_adapter.py
    boltz_wrapper.py
    affinity_preview.py
  monitoring/             # 运行态监控
    task_monitor.py
  routes/                 # 按业务域拆分 API 路由
    prediction.py
    affinity.py
    lead_opt.py
    lead_opt_mmp.py
    task.py
    admin.py
    mmp_lifecycle.py
  services/               # 无状态/低状态服务逻辑
    common_utils.py
    result_archive.py
    mmp_service.py
  scheduling/             # 调度与队列功能路由
    capability_router.py

capabilities/             # 各计算功能的实现代码与运行脚本
  lead_optimization/
  designer/
  colabfold_server/
  pocketxmol/
  affinity/
  boltz2score/
```

> Protenix 运行目录与公共缓存迁移到宿主机 `/data/protenix`，不再占用 `capabilities/`。

## 设计约束

- `backend/routes/*` 只处理 API 参数校验、HTTP 响应、任务入队。
- `backend/services/*` 负责可复用业务逻辑。
- `backend/scheduling/*` 只负责调度策略与 worker 功能识别。
- 根目录历史 `api_server_*` 包装文件已移除，统一从 `backend/` 导入。
- 根目录不再保留 `lead_optimization` 兼容 shim，功能代码统一放在 `capabilities/`。

## 调度稳定性策略

- 功能专属队列，避免异构 worker 抢错任务。
- `worker_prefetch_multiplier=1`，减少队列饥饿。
- `task_acks_late=True` + `task_reject_on_worker_lost=True`，降低 worker 崩溃导致任务丢失概率。
- API 端对无在线功能 worker 的请求快速失败（503），避免“排入错误队列后长时间阻塞”。
