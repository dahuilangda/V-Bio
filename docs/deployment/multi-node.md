# 多节点部署

中央 API、Redis、worker 和 MSA 可以分机部署。所有配置写入 `deploy/docker/DOCKER_STACK_*.env`。

## Redis

```bash
cd /data/V-Bio
cp deploy/docker/DOCKER_STACK_REDIS.env.example deploy/docker/DOCKER_STACK_REDIS.env
docker compose -f deploy/docker/DOCKER_STACK_REDIS.compose.yml   --env-file deploy/docker/DOCKER_STACK_REDIS.env up -d
```

## 中央 API

```bash
cp deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env.example deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
# 修改 BOLTZ_API_TOKEN, REDIS_URL, LEAD_OPT_MMP_DB_URL, MSA_SERVER_URL
docker compose -f deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.compose.yml   --env-file deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env up -d --build
```

## GPU worker

```bash
cp deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env.example deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env
# 修改 BOLTZ_API_TOKEN, REDIS_URL, CENTRAL_API_URL, GPU_WORKER_CAPABILITIES, MSA_SERVER_URL
docker compose -f deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.compose.yml   --env-file deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env   --profile boltz2 --profile alphafold3   up -d --build
```

可用 profile：`boltz2`, `boltz2score`, `affinity`, `alphafold3`, `protenix`, `pocketxmol`。

## CPU worker

```bash
cp deploy/docker/DOCKER_STACK_WORKER_CPU.env.example deploy/docker/DOCKER_STACK_WORKER_CPU.env
# 修改 BOLTZ_API_TOKEN, REDIS_URL, CENTRAL_API_URL, CPU_WORKER_CAPABILITIES, LEAD_OPT_MMP_DB_URL
docker compose -f deploy/docker/DOCKER_STACK_WORKER_CPU.compose.yml   --env-file deploy/docker/DOCKER_STACK_WORKER_CPU.env up -d --build
```

## MSA 服务

```bash
cd /data/V-Bio/deploy/docker
cp DOCKER_CAP_COLABFOLD_SERVER.env.example DOCKER_CAP_COLABFOLD_SERVER.env
docker compose -f DOCKER_CAP_COLABFOLD_SERVER.compose.yml   --env-file DOCKER_CAP_COLABFOLD_SERVER.env up -d --build
```

中央和相关 worker 使用同一个 MSA 地址：

```env
MSA_SERVER_URL=http://<msa-host>:8080
MSA_SERVER_MODE=colabfold
MSA_SERVER_TIMEOUT_SECONDS=1800
```

## 验证

```bash
curl -H "X-API-Token: <TOKEN>" http://<CENTRAL_HOST>:5000/workers/capabilities
curl -H "X-API-Token: <TOKEN>" http://<CENTRAL_HOST>:5000/workers/cluster_status
```
