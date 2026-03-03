# 单机部署（10条命令）

适用场景：只用一台机器同时跑中央服务、GPU worker、CPU worker、frontend。  
默认能力：GPU 先开 `boltz2`，CPU 开 `lead_opt`。

先设置：
```bash
export API_TOKEN='change-me'
export HOST_IP='172.17.3.200'
```

## 先决条件

- 已安装 Docker + Docker Compose + Node.js 18+
- GPU 机器已安装 NVIDIA 驱动与容器运行时
- `/data/boltz_cache` 已准备 `boltz2_conf.ckpt`、`boltz2_aff.ckpt`、`ccd.pkl`、`mols.tar`

## 10 条命令

1.
```bash
cd /data && git clone https://github.com/dahuilangda/V-Bio && cd /data/V-Bio
```

2.
```bash
cp deploy/docker/DOCKER_STACK_REDIS.env.example deploy/docker/DOCKER_STACK_REDIS.env && cp deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env.example deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env && cp deploy/docker/DOCKER_STACK_WORKER_GPU.env.example deploy/docker/DOCKER_STACK_WORKER_GPU.env && cp deploy/docker/DOCKER_STACK_WORKER_CPU.env.example deploy/docker/DOCKER_STACK_WORKER_CPU.env
```

3.
```bash
sed -i "s/^BOLTZ_API_TOKEN=.*/BOLTZ_API_TOKEN=${API_TOKEN}/" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env && sed -i "s#^REDIS_URL=.*#REDIS_URL=redis://${HOST_IP}:6379/0#" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env && sed -i "s#^LEAD_OPT_MMP_DB_URL=.*#LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@${HOST_IP}:54330/leadopt_mmp#" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env && sed -i "s#^LEAD_OPT_MMP_DB_SCHEMA=.*#LEAD_OPT_MMP_DB_SCHEMA=chembl_cyp3a4_herg#" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
```

4.
```bash
sed -i "s/^BOLTZ_API_TOKEN=.*/BOLTZ_API_TOKEN=${API_TOKEN}/" deploy/docker/DOCKER_STACK_WORKER_GPU.env && sed -i "s#^REDIS_URL=.*#REDIS_URL=redis://${HOST_IP}:6379/0#" deploy/docker/DOCKER_STACK_WORKER_GPU.env && sed -i "s#^CENTRAL_API_URL=.*#CENTRAL_API_URL=http://${HOST_IP}:5000#" deploy/docker/DOCKER_STACK_WORKER_GPU.env && sed -i "s/^GPU_WORKER_CAPABILITIES=.*/GPU_WORKER_CAPABILITIES=boltz2/" deploy/docker/DOCKER_STACK_WORKER_GPU.env
```

5.
```bash
cp deploy/docker/DOCKER_CAP_MMP_POSTGRES.env.example deploy/docker/DOCKER_CAP_MMP_POSTGRES.env && docker compose -f deploy/docker/DOCKER_CAP_MMP_POSTGRES.compose.yml --env-file deploy/docker/DOCKER_CAP_MMP_POSTGRES.env up -d --build
```

6.
```bash
sed -i "s/^BOLTZ_API_TOKEN=.*/BOLTZ_API_TOKEN=${API_TOKEN}/" deploy/docker/DOCKER_STACK_WORKER_CPU.env && sed -i "s#^REDIS_URL=.*#REDIS_URL=redis://${HOST_IP}:6379/0#" deploy/docker/DOCKER_STACK_WORKER_CPU.env && sed -i "s#^CENTRAL_API_URL=.*#CENTRAL_API_URL=http://${HOST_IP}:5000#" deploy/docker/DOCKER_STACK_WORKER_CPU.env && sed -i "s#^LEAD_OPT_MMP_DB_URL=.*#LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@${HOST_IP}:54330/leadopt_mmp#" deploy/docker/DOCKER_STACK_WORKER_CPU.env
```

7.
```bash
docker build -f deploy/docker/DOCKER_BOLTZ2_RUNTIME.Dockerfile -t vbio-boltz2-runtime .
```

8.
```bash
docker compose -f deploy/docker/DOCKER_STACK_REDIS.compose.yml --env-file deploy/docker/DOCKER_STACK_REDIS.env up -d && docker compose -f deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.compose.yml --env-file deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env up -d --build
```

9.
```bash
docker compose -f deploy/docker/DOCKER_STACK_WORKER_GPU.compose.yml --env-file deploy/docker/DOCKER_STACK_WORKER_GPU.env up -d --build && docker compose -f deploy/docker/DOCKER_STACK_WORKER_CPU.compose.yml --env-file deploy/docker/DOCKER_STACK_WORKER_CPU.env up -d --build
```

10.
```bash
cd frontend/supabase-lite && docker compose up -d && cd .. && cp .env.example .env && sed -i "s#^VITE_API_BASE_URL=.*#VITE_API_BASE_URL=http://${HOST_IP}:5000#" .env && sed -i "s#^VITE_API_TOKEN=.*#VITE_API_TOKEN=${API_TOKEN}#" .env && npm install && npm run dev
```

## 验证

```bash
curl -H "X-API-Token: ${API_TOKEN}" "http://${HOST_IP}:5000/workers/capabilities"
curl -H "X-API-Token: ${API_TOKEN}" "http://${HOST_IP}:5000/workers/cluster_status"
```

如果返回中有 `gpu@...` 和 `cpu@...`，且能力符合各自 stack env 声明，说明单机部署成功。
