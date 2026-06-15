# 单机部署

目标：在一台机器上启动 Redis、中央 API、GPU worker、CPU worker、前端和 management API。

## 1. 准备 env

```bash
cd /data/V-Bio
cp deploy/docker/DOCKER_STACK_REDIS.env.example deploy/docker/DOCKER_STACK_REDIS.env
cp deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env.example deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
cp deploy/docker/DOCKER_STACK_WORKER_GPU.env.example deploy/docker/DOCKER_STACK_WORKER_GPU.env
cp deploy/docker/DOCKER_STACK_WORKER_CPU.env.example deploy/docker/DOCKER_STACK_WORKER_CPU.env
cp frontend/.env.example frontend/.env
```

至少修改：

```text
deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env: BOLTZ_API_TOKEN, REDIS_URL, LEAD_OPT_MMP_DB_URL
deploy/docker/DOCKER_STACK_WORKER_GPU.env: BOLTZ_API_TOKEN, REDIS_URL, CENTRAL_API_URL, GPU_WORKER_CAPABILITIES
deploy/docker/DOCKER_STACK_WORKER_CPU.env: BOLTZ_API_TOKEN, REDIS_URL, CENTRAL_API_URL, CPU_WORKER_CAPABILITIES
frontend/.env: VITE_API_BASE_URL, VITE_API_TOKEN, VITE_SUPER_ADMIN_USERNAMES, VITE_SUPER_ADMIN_EMAILS, VBIO_SESSION_SECRET
```

地址示例：

```env
REDIS_URL=redis://<HOST_IP>:6379/0
CENTRAL_API_URL=http://<HOST_IP>:5000
VITE_API_BASE_URL=http://<HOST_IP>:5000
VITE_API_TOKEN=<BOLTZ_API_TOKEN>
```

## 2. 启动后端

```bash
docker compose -f deploy/docker/DOCKER_STACK_REDIS.compose.yml   --env-file deploy/docker/DOCKER_STACK_REDIS.env up -d

docker compose -f deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.compose.yml   --env-file deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env up -d --build

docker compose -f deploy/docker/DOCKER_STACK_WORKER_GPU.compose.yml   --env-file deploy/docker/DOCKER_STACK_WORKER_GPU.env up -d --build

docker compose -f deploy/docker/DOCKER_STACK_WORKER_CPU.compose.yml   --env-file deploy/docker/DOCKER_STACK_WORKER_CPU.env up -d --build
```

模型服务的镜像、权重和数据目录见 `docs/deployment/model-services.md`。

## 3. 启动前端

```bash
cd /data/V-Bio/frontend/supabase-lite
docker compose up -d

cd /data/V-Bio
bash frontend/run.sh start
```

## 4. 验证

```bash
curl -H "X-API-Token: <BOLTZ_API_TOKEN>" http://<HOST_IP>:5000/workers/capabilities
curl -H "X-API-Token: <BOLTZ_API_TOKEN>" http://<HOST_IP>:5000/workers/cluster_status
```

前端默认地址：`http://127.0.0.1:5173`。
