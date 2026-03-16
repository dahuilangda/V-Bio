# Docker 安装入口（统一）

本目录是 V-Bio 后端 Docker 安装与部署的唯一入口。

配置边界说明：
- 微服务部署请以 `DOCKER_STACK_*.env` 作为唯一配置源。
- 容器配置统一在对应 stack 的 env 文件中维护，不依赖仓库根目录 `.env`。

## 1. 平台栈（中央服务器 / GPU Worker / CPU Worker）

- `DOCKER_BACKEND_RUNTIME.Dockerfile`
- `DOCKER_STACK_CENTRAL.compose.yml`
- `DOCKER_STACK_CENTRAL.env.example`
- `DOCKER_STACK_CENTRAL_DECOUPLED.compose.yml`（中央服务不内置 Redis）
- `DOCKER_STACK_CENTRAL_DECOUPLED.env.example`
- `DOCKER_STACK_REDIS.compose.yml`（独立 Redis 栈）
- `DOCKER_STACK_REDIS.env.example`
- `DOCKER_STACK_WORKER_GPU.compose.yml`
- `DOCKER_STACK_WORKER_GPU.env.example`
- `DOCKER_STACK_WORKER_GPU_CAPS.compose.yml`（按 capability 拆分的 GPU worker 微服务栈）
- `DOCKER_STACK_WORKER_GPU_CAPS.env.example`
- `DOCKER_STACK_WORKER_CPU.compose.yml`
- `DOCKER_STACK_WORKER_CPU.env.example`

补充：
- 多肽设计父编排任务建议像 `lead_opt` 一样由 CPU worker 承接（`CPU_WORKER_CAPABILITIES=lead_opt,peptide_design`）。
- 多肽设计默认建议将 `PEPTIDE_PARENT_SUBPROCESS_TIMEOUT_SECONDS=0`、`PEPTIDE_CANDIDATE_SUBPROCESS_TIMEOUT_SECONDS=0`、`PEPTIDE_GPU_ACQUIRE_TIMEOUT_SECONDS=0`，避免多父任务/多子任务共享 GPU 时因排队被误判超时。
- 如你仍需要兜底超时，可在 `DOCKER_STACK_WORKER_CPU.env` 重新设置上述参数为正整数，并结合 `PEPTIDE_PARENT_TIMEOUT_PER_WAVE_SECONDS`、`PEPTIDE_PARENT_TIMEOUT_BUFFER_SECONDS` 调整预算。

## 2. 功能镜像

- `DOCKER_BOLTZ2_RUNTIME.Dockerfile`：`boltz2` / `boltz2score` / `affinity` 统一运行时
- `DOCKER_CAP_ALPHAFOLD3.env.example`
- `DOCKER_CAP_PROTENIX.env.example`
- `DOCKER_CAP_COLABFOLD_SERVER.Dockerfile`
- `DOCKER_CAP_COLABFOLD_SERVER.compose.yml`
- `DOCKER_CAP_COLABFOLD_SERVER.env.example`
- `DOCKER_CAP_POCKETXMOL.Dockerfile`
- `DOCKER_CAP_POCKETXMOL.compose.yml`
- `DOCKER_CAP_POCKETXMOL.env.example`
- `DOCKER_CAP_MMP_POSTGRES.Dockerfile`
- `DOCKER_CAP_MMP_POSTGRES.compose.yml`
- `DOCKER_CAP_MMP_POSTGRES.env.example`
- `DOCKER_CAP_MMP_POSTGRES.init.sql`
