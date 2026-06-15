# Docker 文件

Docker 部署只使用对应 stack 的 env 文件，不依赖仓库根目录 `.env`。

## 平台栈

| 文件 | 用途 |
| --- | --- |
| `DOCKER_STACK_REDIS.compose.yml` | Redis |
| `DOCKER_STACK_CENTRAL_DECOUPLED.compose.yml` | 中央 API，不内置 Redis |
| `DOCKER_STACK_CENTRAL.compose.yml` | 中央 API，带本机 Redis 的旧入口 |
| `DOCKER_STACK_WORKER_GPU.compose.yml` | 单个 GPU worker |
| `DOCKER_STACK_WORKER_GPU_CAPS.compose.yml` | 按服务拆分的 GPU worker |
| `DOCKER_STACK_WORKER_CPU.compose.yml` | CPU worker |

每个 compose 文件都有对应的 `.env.example`。复制为 `.env` 后再启动。

## 模型服务镜像

| 文件 | 用途 |
| --- | --- |
| `DOCKER_BOLTZ2_RUNTIME.Dockerfile` | `boltz2`, `boltz2score`, `affinity` |
| `DOCKER_PROTENIX_V2_RUNTIME.Dockerfile` | Protenix v2 runtime |
| `DOCKER_CAP_POCKETXMOL.*` | PocketXMol |
| `DOCKER_CAP_COLABFOLD_SERVER.*` | ColabFold MSA 服务 |
| `DOCKER_CAP_MMP_POSTGRES.*` | Lead Opt / MMP PostgreSQL |

部署步骤见 `docs/deployment/quick-start.md`，模型服务安装见 `docs/deployment/model-services.md`。
