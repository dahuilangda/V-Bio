# 环境变量

## 后端与 worker

| 变量 | 用途 |
| --- | --- |
| `BOLTZ_API_TOKEN` | 后端 API 访问 token。前端的 `VITE_API_TOKEN` 必须与它一致。 |
| `REDIS_URL` | Redis 地址，例如 `redis://<HOST_IP>:6379/0`。 |
| `CENTRAL_API_URL` | 中央 API 地址，例如 `http://<HOST_IP>:5000`。 |
| `GPU_WORKER_CAPABILITIES` | GPU worker 服务列表，例如 `boltz2,alphafold3,protenix`。 |
| `CPU_WORKER_CAPABILITIES` | CPU worker 服务列表，例如 `lead_opt,peptide_design`。 |

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
| Protenix | `PROTENIX_DOCKER_IMAGE`, `PROTENIX_SOURCE_DIR`, `PROTENIX_MODEL_DIR` |
| PocketXMol | `POCKETXMOL_DOCKER_IMAGE`, `POCKETXMOL_ROOT_DIR` |
| ColabFold MSA | `MSA_SERVER_URL`, `COLABFOLD_JOBS_DIR` |
| Lead Opt | `LEAD_OPT_MMP_DB_URL`, `LEAD_OPT_MMP_DB_SCHEMA` |

安装命令见：

```text
docs/deployment/model-services.md
```
