# 微服务解耦部署（中央 / Redis / Capability / MSA）

目标：将中央调度、Redis、GPU capability worker、CPU worker、MSA 服务拆为独立栈，支持跨机器部署与按能力横向扩缩容。

说明：本方案只使用 `deploy/docker/DOCKER_STACK_*.env`；不依赖仓库根目录 `.env`。

## 1. Redis 独立部署

```bash
cd /data/V-Bio
cp deploy/docker/DOCKER_STACK_REDIS.env.example deploy/docker/DOCKER_STACK_REDIS.env
docker compose -f deploy/docker/DOCKER_STACK_REDIS.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_REDIS.env up -d
```

## 2. 中央服务（不内置 Redis）

```bash
cd /data/V-Bio
cp deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env.example deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
# 至少修改: BOLTZ_API_TOKEN, REDIS_URL, LEAD_OPT_MMP_DB_URL, MSA_SERVER_URL
docker compose -f deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env up -d --build
```

## 3. GPU capability 微服务（按 profile 启动）

```bash
cd /data/V-Bio
cp deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env.example deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
# 至少修改: BOLTZ_API_TOKEN, REDIS_URL, CENTRAL_API_URL, MSA_SERVER_URL
```

示例：只启动 `boltz2` 与 `alphafold3` 两个独立 worker 服务

```bash
docker compose -f deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env \
  --profile boltz2 \
  --profile alphafold3 \
  up -d --build
```

可选 profile：
- `boltz2`
- `boltz2score`
- `affinity`
- `alphafold3`
- `protenix`
- `pocketxmol`

实现说明：
- 每个 capability worker 都会设置独立 `GPU_POOL_NAMESPACE`，GPU 池 Redis 键隔离，避免初始化相互覆盖。
- 默认可不设置 `GPU_DEVICE_IDS_*`：多个 capability 会共享/争抢 GPU，以提升总体利用率。
- 如需资源隔离、降低互相影响，再在 `DOCKER_STACK_WORKER_GPU_CAPS.env` 显式设置不同的 `GPU_DEVICE_IDS_*`（可选）。

## 4. CPU worker（Lead Opt / MMP）

```bash
cd /data/V-Bio
cp deploy/docker/DOCKER_STACK_WORKER_CPU.env.example deploy/docker/DOCKER_STACK_WORKER_CPU.env
# 至少修改: BOLTZ_API_TOKEN, REDIS_URL, CENTRAL_API_URL, LEAD_OPT_MMP_DB_URL
docker compose -f deploy/docker/DOCKER_STACK_WORKER_CPU.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_WORKER_CPU.env up -d --build
```

## 5. ColabFold MSA 服务独立部署

```bash
cd /data/V-Bio/deploy/docker
cp DOCKER_CAP_COLABFOLD_SERVER.env.example DOCKER_CAP_COLABFOLD_SERVER.env
docker compose -f DOCKER_CAP_COLABFOLD_SERVER.compose.yml \
  --env-file DOCKER_CAP_COLABFOLD_SERVER.env up -d --build
```

并确保中央与相关 worker 的环境变量都指向该服务：

```env
MSA_SERVER_URL=http://<msa-host>:8080
MSA_SERVER_MODE=colabfold
MSA_SERVER_TIMEOUT_SECONDS=1800
```

## 6. 验证

```bash
curl -H "X-API-Token: <TOKEN>" http://<CENTRAL_HOST>:5000/workers/capabilities
curl -H "X-API-Token: <TOKEN>" http://<CENTRAL_HOST>:5000/workers/cluster_status
```

预期：
- 每个 capability 显示为独立 worker 在线。
- `lead_opt` 由 CPU worker 提供。
- `peptide_design` 由声明了 `CPU_WORKER_CAPABILITIES=...peptide_design` 的 CPU worker 提供。
- 未部署的 capability 提交任务会返回 503（不会落入旧通用队列）。
