# 环境变量

## 后端与 worker

| 变量 | 用途 |
| --- | --- |
| `BOLTZ_API_TOKEN` | 后端 API 访问 token。前端的 `VITE_API_TOKEN` 必须与它一致。 |
| `REDIS_URL` | Redis 地址，例如 `redis://<HOST_IP>:6379/0`。 |
| `CENTRAL_API_URL` | 中央 API 地址，例如 `http://<HOST_IP>:5000`。 |
| `GPU_WORKER_CAPABILITIES` | GPU worker 服务列表，例如 `boltz2,alphafold3,protenix,nesso`。 |
| `CPU_WORKER_CAPABILITIES` | CPU worker 服务列表，例如 `lead_opt,peptide_design,export`（`export` 承接任务列表 Excel 异步导出）。 |
| `RESULTS_BASE_DIR` | 中心结果目录，默认 `/data/boltz_central_results`。任务结果 zip 平铺在根目录，中间结果树按 `<backend>/<task_id>/` 存放。 |
| `RESULTS_RETENTION_DAYS` | 任务结果保留天数，默认 `90`（约 3 个月）。超过该期限的任务结果（结果 zip、中间结果树、lead_opt 输出、泄漏的临时目录）会被自动删除。 |
| `RESULTS_CLEANUP_ENABLED` | 结果清理开关，默认开启（`1/true/yes/on`）。 |
| `RESULTS_CLEANUP_INTERVAL_SECONDS` | 结果清理执行间隔（秒），默认 `21600`（6 小时）；monitor 服务启动后会先执行一轮。 |
| `LEAD_OPTIMIZATION_OUTPUT_DIR` | lead optimization 输出目录，默认 `/data/boltz_lead_optimization_results`。 |
| `BOLTZ_MSA_CACHE_DIR` | MSA(a3m) 序列缓存目录，默认 `/data/boltz_msa_cache`。 |
| `MSA_CACHE_RETENTION_DAYS` | MSA 缓存保留天数，默认 `90`。缓存可整体重建，超过该期限的文件由同一清理周期删除。 |

清理同时受活动任务保护：`monitor_tasks_current` 中排队/运行中的任务不会被删除。亦可手动触发：

```bash
# 仅统计可删项与预计释放空间，不删除
python -m backend.monitoring.result_cleanup --dry-run
# 实际执行
python -m backend.monitoring.result_cleanup --run
```

## 前端与 management API

配置文件：`frontend/.env`

| 变量 | 用途 |
| --- | --- |
| `VITE_API_BASE_URL` | 后端 API 地址。 |
| `VITE_API_TOKEN` | 与 `BOLTZ_API_TOKEN` 保持一致。 |
| `VITE_SUPABASE_REST_URL` | PostgREST 地址，默认 `http://127.0.0.1:54321`。 |
| `VITE_SUPER_ADMIN_USERNAMES` | 超级管理员用户名，逗号分隔。 |
| `VITE_SUPER_ADMIN_EMAILS` | 超级管理员邮箱，逗号分隔。 |
| `VBIO_JWT_CLIENTS_FILE` | 外部系统接入配置文件，默认 `frontend/.run/jwt_clients.json`。 |
| `VBIO_SESSION_SECRET` | management API 会话签名密钥，只放服务端。 |

当前超级管理员：

```env
VITE_SUPER_ADMIN_USERNAMES=dahuilangda
VITE_SUPER_ADMIN_EMAILS=dahuilangda@hotmail.com
```

## 外部系统登录

外部系统登录使用短期 JWT。接入步骤、JWT 字段和签名示例见：

```text
docs/apis/external-system-login.md
```

## 模型服务

常用模型服务变量：

| 服务 | 关键变量 |
| --- | --- |
| Boltz2 | `BOLTZ2_DOCKER_IMAGE`, `BOLTZ2_HOST_CACHE_DIR` |
| AlphaFold3 | `ALPHAFOLD3_DOCKER_IMAGE`, `ALPHAFOLD3_MODEL_DIR`, `ALPHAFOLD3_DATABASE_DIR` |
| Protenix | `PROTENIX_DOCKER_IMAGE`, `PROTENIX_SOURCE_DIR`, `PROTENIX_MODEL_DIR`, `PROTENIX_COMMON_CACHE_DIR`, `PROTENIX_MODULE_CACHE_DIR` |
| Nesso-1 Virtual Screening | `NESSO_DOCKER_IMAGE`, `NESSO_HOST_CACHE_DIR`, `NESSO_MODEL_REVISION` |
| ColabFold MSA | `MSA_SERVER_URL`, `COLABFOLD_JOBS_DIR` |

安装命令见：

```text
docs/deployment/model-services.md
```
