# 单机安装全部应用（Docker + 前端）

在一台机器上启动完整 V-Bio 服务，包含：
- Redis（独立服务）
- 中央服务（API + Monitor）
- GPU 能力（`boltz2` / `boltz2score` / `affinity` / `alphafold3` / `protenix` / `pocketxmol`）
- CPU 能力（`lead_opt` + `peptide_design`）
- MMP PostgreSQL
- ColabFold MSA 服务
- 前端（supabase-lite + management API + web）

说明：本流程只使用 `deploy/docker/DOCKER_STACK_*.env`（以及 `frontend/.env`）。

## 0. 前置条件

```bash
cd /data
git clone https://github.com/dahuilangda/V-Bio
cd /data/V-Bio
```

需要：
- Docker + Docker Compose
- NVIDIA 驱动与 `nvidia-container-toolkit`
- 可用 GPU（用于 GPU worker）

先设置两个环境变量（仅这两个）：

```bash
export API_TOKEN='woaihuadong'
export HOST_IP='172.17.3.200'
```

其余配置全部写入各服务的 `.env` 文件。

## 1. 启动 Redis（独立服务）

```bash
cd /data/V-Bio
cp deploy/docker/DOCKER_STACK_REDIS.env.example deploy/docker/DOCKER_STACK_REDIS.env
docker compose -f deploy/docker/DOCKER_STACK_REDIS.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_REDIS.env up -d
```

Redis 连通性测试：

```bash
docker compose -f deploy/docker/DOCKER_STACK_REDIS.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_REDIS.env ps
docker compose -f deploy/docker/DOCKER_STACK_REDIS.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_REDIS.env exec redis redis-cli ping
```

## 2. 启动 ColabFold MSA 服务

```bash
cd /data/V-Bio/deploy/docker
cp DOCKER_CAP_COLABFOLD_SERVER.env.example DOCKER_CAP_COLABFOLD_SERVER.env
docker compose -f DOCKER_CAP_COLABFOLD_SERVER.compose.yml \
  --env-file DOCKER_CAP_COLABFOLD_SERVER.env up -d --build
```

ColabFold API 测试（应返回 ticket JSON）：

```bash
curl -sS -X POST \
  -d 'q=>query\nMKFLILLFNILCLFPVLAADNHGVGP' \
  -d 'mode=colabfold' \
  "http://${HOST_IP}:8080/ticket/msa"
```

## 3. 启动 MMP PostgreSQL

```bash
cd /data/V-Bio/deploy/docker
cp DOCKER_CAP_MMP_POSTGRES.env.example DOCKER_CAP_MMP_POSTGRES.env
docker compose -f DOCKER_CAP_MMP_POSTGRES.compose.yml \
  --env-file DOCKER_CAP_MMP_POSTGRES.env up -d --build
```

MMP PostgreSQL 连通性测试：

```bash
docker compose -f DOCKER_CAP_MMP_POSTGRES.compose.yml \
  --env-file DOCKER_CAP_MMP_POSTGRES.env ps
docker compose -f DOCKER_CAP_MMP_POSTGRES.compose.yml \
  --env-file DOCKER_CAP_MMP_POSTGRES.env exec leadopt-mmp-db \
  pg_isready -U leadopt -d leadopt_mmp
docker compose -f DOCKER_CAP_MMP_POSTGRES.compose.yml \
  --env-file DOCKER_CAP_MMP_POSTGRES.env exec leadopt-mmp-db \
  psql -U leadopt -d leadopt_mmp -c 'select 1;'
```

## 4. 启动中央栈（API + Monitor）

```bash
cd /data/V-Bio
cp deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env.example deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
sed -i "s/^BOLTZ_API_TOKEN=.*/BOLTZ_API_TOKEN=${API_TOKEN}/" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
sed -i "s#^REDIS_URL=.*#REDIS_URL=redis://${HOST_IP}:6379/0#" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
sed -i "s#^LEAD_OPT_MMP_DB_URL=.*#LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@${HOST_IP}:54330/leadopt_mmp#" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
sed -i "s#^LEAD_OPT_MMP_DB_SCHEMA=.*#LEAD_OPT_MMP_DB_SCHEMA=chembl_cyp3a4_herg#" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
sed -i "s#^MSA_SERVER_URL=.*#MSA_SERVER_URL=http://${HOST_IP}:8080#" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
sed -i "s#^MSA_SERVER_TIMEOUT_SECONDS=.*#MSA_SERVER_TIMEOUT_SECONDS=1800#" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
docker compose -f deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env up -d --build
```

中央 API / Monitor 测试：

```bash
curl -sS -H "X-API-Token: ${API_TOKEN}" \
  "http://${HOST_IP}:5000/workers/capabilities"
curl -sS -H "X-API-Token: ${API_TOKEN}" \
  "http://${HOST_IP}:5000/monitor/status"
```

## 4.5 安装 AlphaFold3 / Protenix / PocketXMol（首次部署必做）

> 下面只做能力安装，不启动 worker。完成后再执行第 5 步。

AlphaFold3（镜像 + 目录）：

```bash
docker pull cford38/alphafold3
mkdir -p /data/alphafold3/models /data/alphafold3/databases
# 把 AF3 官方模型与数据库下载到上述目录后再继续
```

Protenix（官方镜像 + 源码/权重/common）：

```bash
docker pull ai4s-share-public-cn-beijing.cr.volces.com/release/protenix:1.0.0.4
mkdir -p /data/protenix
git clone https://github.com/bytedance/Protenix.git /data/protenix/source
cd /data/protenix/source
python3 -m protenix.data.download_protenix_data --data part_weights --download_dir /data/protenix
python3 -m protenix.data.download_protenix_data --data part_data --download_dir /data/protenix
```

PocketXMol（独立镜像 + 权重）：

```bash
cd /data/V-Bio/deploy/docker
docker compose -f DOCKER_CAP_POCKETXMOL.compose.yml build pocketxmol
cd /data/V-Bio/capabilities/pocketxmol
mkdir -p weights
# 下载 model_weights.tar.gz 后解压
tar -zxvf model_weights.tar.gz -C .
```

## 5. 准备并启动 GPU 全能力 worker

先构建 Boltz 统一运行时镜像：

```bash
cd /data/V-Bio
docker build -f deploy/docker/DOCKER_BOLTZ2_RUNTIME.Dockerfile -t vbio-boltz2-runtime .
```

准备 GPU worker 配置：

```bash
cp deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env.example deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s/^BOLTZ_API_TOKEN=.*/BOLTZ_API_TOKEN=${API_TOKEN}/" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s#^REDIS_URL=.*#REDIS_URL=redis://${HOST_IP}:6379/0#" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s#^CENTRAL_API_URL=.*#CENTRAL_API_URL=http://${HOST_IP}:5000#" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s#^MSA_SERVER_URL=.*#MSA_SERVER_URL=http://${HOST_IP}:8080#" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s#^MSA_SERVER_TIMEOUT_SECONDS=.*#MSA_SERVER_TIMEOUT_SECONDS=1800#" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
# 可选：显式声明 AF3/Protenix/PocketXMol 运行时路径与镜像
sed -i "s#^ALPHAFOLD3_ROOT_HOST=.*#ALPHAFOLD3_ROOT_HOST=/data/alphafold3#" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s#^ALPHAFOLD3_MODEL_DIR=.*#ALPHAFOLD3_MODEL_DIR=/data/alphafold3/models#" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s#^ALPHAFOLD3_DATABASE_DIR=.*#ALPHAFOLD3_DATABASE_DIR=/data/alphafold3/databases#" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s#^PROTENIX_SOURCE_DIR_HOST=.*#PROTENIX_SOURCE_DIR_HOST=/data/protenix#" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s#^PROTENIX_SOURCE_DIR=.*#PROTENIX_SOURCE_DIR=/data/protenix#" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s#^PROTENIX_MODEL_DIR=.*#PROTENIX_MODEL_DIR=/data/protenix/model#" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s#^PROTENIX_COMMON_CACHE_DIR=.*#PROTENIX_COMMON_CACHE_DIR=/data/protenix/common_cache#" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
sed -i "s#^POCKETXMOL_DOCKER_IMAGE=.*#POCKETXMOL_DOCKER_IMAGE=pocketxmol:cu128#" deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
```

可选：按实际机器显式划分 GPU（用于资源隔离/稳定性调优；默认留空可共享争抢）：

```env
GPU_DEVICE_IDS_BOLTZ2=0
GPU_DEVICE_IDS_BOLTZ2SCORE=0
GPU_DEVICE_IDS_AFFINITY=0
GPU_DEVICE_IDS_ALPHAFOLD3=1
GPU_DEVICE_IDS_PROTENIX=2
GPU_DEVICE_IDS_POCKETXMOL=3
```

启动全部 GPU profiles：

```bash
docker compose -f deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env \
  --profile boltz2 \
  --profile boltz2score \
  --profile affinity \
  --profile alphafold3 \
  --profile protenix \
  --profile pocketxmol \
  up -d --build
```

GPU worker 接入测试（应看到 `boltz2/affinity/alphafold3/protenix/pocketxmol`）：

```bash
curl -sS -H "X-API-Token: ${API_TOKEN}" \
  "http://${HOST_IP}:5000/workers/cluster_status"
```

## 6. 启动 CPU worker（lead_opt + peptide_design）

```bash
cp deploy/docker/DOCKER_STACK_WORKER_CPU.env.example deploy/docker/DOCKER_STACK_WORKER_CPU.env
sed -i "s/^BOLTZ_API_TOKEN=.*/BOLTZ_API_TOKEN=${API_TOKEN}/" deploy/docker/DOCKER_STACK_WORKER_CPU.env
sed -i "s#^REDIS_URL=.*#REDIS_URL=redis://${HOST_IP}:6379/0#" deploy/docker/DOCKER_STACK_WORKER_CPU.env
sed -i "s#^CENTRAL_API_URL=.*#CENTRAL_API_URL=http://${HOST_IP}:5000#" deploy/docker/DOCKER_STACK_WORKER_CPU.env
sed -i "s#^LEAD_OPT_MMP_DB_URL=.*#LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@${HOST_IP}:54330/leadopt_mmp#" deploy/docker/DOCKER_STACK_WORKER_CPU.env
sed -i "s#^CPU_WORKER_CAPABILITIES=.*#CPU_WORKER_CAPABILITIES=lead_opt,peptide_design#" deploy/docker/DOCKER_STACK_WORKER_CPU.env
docker compose -f deploy/docker/DOCKER_STACK_WORKER_CPU.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_WORKER_CPU.env up -d --build
```

CPU worker + lead_opt API 测试：

```bash
curl -sS -H "X-API-Token: ${API_TOKEN}" \
  "http://${HOST_IP}:5000/workers/cluster_status"
curl -sS -H "X-API-Token: ${API_TOKEN}" \
  "http://${HOST_IP}:5000/api/lead_optimization/mmp_databases"
```

## 7. 启动前端

```bash
# 先停掉旧实例，避免 5173 被历史进程占用
cd /data/V-Bio
bash frontend/run.sh stop || true
pkill -f '/data/Boltz-WebUI/VBio/node_modules/.bin/vite' || true
pkill -f '/data/V-Bio/frontend/node_modules/.bin/vite' || true

cd /data/V-Bio/frontend/supabase-lite
docker compose up -d

cd /data/V-Bio/frontend
cp .env.example .env
sed -i "s#^VITE_API_BASE_URL=.*#VITE_API_BASE_URL=http://${HOST_IP}:5000#" .env
sed -i "s#^VITE_API_TOKEN=.*#VITE_API_TOKEN=${API_TOKEN}#" .env
npm install
npm run db:migrate
cd /data/V-Bio
bash frontend/run.sh dev
```

## 8. 验证

中央能力快照：

```bash
curl -H "X-API-Token: ${API_TOKEN}" "http://${HOST_IP}:5000/workers/capabilities"
curl -H "X-API-Token: ${API_TOKEN}" "http://${HOST_IP}:5000/workers/cluster_status"
```

你应看到以下 capability 在线：
- `boltz2`
- `boltz2score`
- `affinity`
- `alphafold3`
- `protenix`
- `pocketxmol`
- `lead_opt`
- `peptide_design`

按旧版 README 风格补充 API 测试（区分认证）：

需要 `X-API-Token` 的管理接口：

```bash
# Worker/队列视图
curl -sS -H "X-API-Token: ${API_TOKEN}" "http://${HOST_IP}:5000/tasks"
curl -sS -H "X-API-Token: ${API_TOKEN}" "http://${HOST_IP}:5000/workers/capabilities"
curl -sS -H "X-API-Token: ${API_TOKEN}" "http://${HOST_IP}:5000/workers/cluster_status"

# 监控视图
curl -sS -H "X-API-Token: ${API_TOKEN}" "http://${HOST_IP}:5000/monitor/status"

# Lead Opt 后端与数据库目录
curl -sS -H "X-API-Token: ${API_TOKEN}" "http://${HOST_IP}:5000/api/lead_optimization/backends"
curl -sS -H "X-API-Token: ${API_TOKEN}" "http://${HOST_IP}:5000/api/lead_optimization/mmp_databases"
```

无需 Token 的公开轮询接口：

```bash
# 将 <task_id> 替换为真实任务 ID
curl -sS "http://${HOST_IP}:5000/status/<task_id>"
curl -OJ "http://${HOST_IP}:5000/results/<task_id>"
```

前端访问：
- `http://127.0.0.1:5173`

## 9. 常见问题

- `peptide_design` 任务返回 503：检查 CPU worker 的 `CPU_WORKER_CAPABILITIES` 是否包含 `peptide_design`。
- `boltz2` 相关任务失败：确认 `vbio-boltz2-runtime` 已构建、`BOLTZ2_HOST_CACHE_DIR` 已挂载并含模型缓存。
- MSA 报错：确认中央与 worker 的 `MSA_SERVER_URL` 都指向 `http://${HOST_IP}:8080`（或你的实际地址），并把 `MSA_SERVER_TIMEOUT_SECONDS` 调大（如 `1800` 或 `3600`）。
- MMP admin/metrics 报错 `connection refused`：确认 `deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env` 和 `deploy/docker/DOCKER_STACK_WORKER_CPU.env` 里的 `LEAD_OPT_MMP_DB_URL` 都指向 `postgresql://leadopt:leadopt@${HOST_IP}:54330/leadopt_mmp`，不要在容器里写 `127.0.0.1`。
