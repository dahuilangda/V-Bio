# V-Bio 极简微服务部署（4段×3步）

目标：最少操作跑通 `中央 + GPU + CPU + frontend`。  
约束：计算节点只按各自 `DOCKER_STACK_WORKER_*.env` 声明能力接任务（`cap.*` 队列）。

如果你只用一台机器，请直接看：
- `docs/deployment/single-node-10-commands.md`

如果你要做“多 capability profile + MSA 细粒度拆分”，请看：
- `docs/deployment/microservice-decoupling.md`

## 0. 先设置两个环境变量

```bash
export API_TOKEN='woaihuadong'
export HOST_IP='172.17.3.200'
```

其余配置全部写入各服务的 `.env` 文件（不要再新增其他 `export`）。

统一在各 stack 的 `.env` 文件里填写：
- `BOLTZ_API_TOKEN=${API_TOKEN}`
- `REDIS_URL=redis://${HOST_IP}:6379/0`
- `CENTRAL_API_URL=http://${HOST_IP}:5000`

## 1. 中央服务器

1. 准备代码
```bash
cd /data && git clone https://github.com/dahuilangda/V-Bio && cd /data/V-Bio
```

2. 准备配置（Redis + 中央解耦栈）
```bash
cp deploy/docker/DOCKER_STACK_REDIS.env.example deploy/docker/DOCKER_STACK_REDIS.env
cp deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env.example deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
sed -i "s/^BOLTZ_API_TOKEN=.*/BOLTZ_API_TOKEN=${API_TOKEN}/" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
sed -i "s#^REDIS_URL=.*#REDIS_URL=redis://${HOST_IP}:6379/0#" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
sed -i "s#^LEAD_OPT_MMP_DB_URL=.*#LEAD_OPT_MMP_DB_URL=postgresql://leadopt:leadopt@${HOST_IP}:54330/leadopt_mmp#" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
sed -i "s#^LEAD_OPT_MMP_DB_SCHEMA=.*#LEAD_OPT_MMP_DB_SCHEMA=chembl_cyp3a4_herg#" deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env
```

3. 启动中央栈（Redis + API + Monitor）
```bash
docker compose -f deploy/docker/DOCKER_STACK_REDIS.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_REDIS.env up -d
docker compose -f deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env up -d --build
```

## 2. GPU 计算节点（3步，先跑 `boltz2`）

1. 构建统一镜像
```bash
cd /data/V-Bio
docker build -f deploy/docker/DOCKER_BOLTZ2_RUNTIME.Dockerfile -t vbio-boltz2-runtime .
```

2. 准备配置
```bash
cp deploy/docker/DOCKER_STACK_WORKER_GPU.env.example deploy/docker/DOCKER_STACK_WORKER_GPU.env
sed -i "s/^BOLTZ_API_TOKEN=.*/BOLTZ_API_TOKEN=${API_TOKEN}/" deploy/docker/DOCKER_STACK_WORKER_GPU.env
sed -i "s#^REDIS_URL=.*#REDIS_URL=redis://${HOST_IP}:6379/0#" deploy/docker/DOCKER_STACK_WORKER_GPU.env
sed -i "s#^CENTRAL_API_URL=.*#CENTRAL_API_URL=http://${HOST_IP}:5000#" deploy/docker/DOCKER_STACK_WORKER_GPU.env
```

3. 启动 GPU worker
```bash
docker compose -f deploy/docker/DOCKER_STACK_WORKER_GPU.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_WORKER_GPU.env up -d --build
```

说明：`BOLTZ2_HOST_CACHE_DIR` 目录需包含 `boltz2_conf.ckpt`、`boltz2_aff.ckpt`、`ccd.pkl`、`mols.tar`。

## 3. CPU 计算节点（3步，`lead_opt`）

1. （可选）本机启动 MMP PostgreSQL
```bash
cp deploy/docker/DOCKER_CAP_MMP_POSTGRES.env.example deploy/docker/DOCKER_CAP_MMP_POSTGRES.env
docker compose -f deploy/docker/DOCKER_CAP_MMP_POSTGRES.compose.yml \
  --env-file deploy/docker/DOCKER_CAP_MMP_POSTGRES.env up -d --build
```

2. 准备 CPU worker 配置
```bash
cp deploy/docker/DOCKER_STACK_WORKER_CPU.env.example deploy/docker/DOCKER_STACK_WORKER_CPU.env
sed -i "s/^BOLTZ_API_TOKEN=.*/BOLTZ_API_TOKEN=${API_TOKEN}/" deploy/docker/DOCKER_STACK_WORKER_CPU.env
sed -i "s#^REDIS_URL=.*#REDIS_URL=redis://${HOST_IP}:6379/0#" deploy/docker/DOCKER_STACK_WORKER_CPU.env
sed -i "s#^CENTRAL_API_URL=.*#CENTRAL_API_URL=http://${HOST_IP}:5000#" deploy/docker/DOCKER_STACK_WORKER_CPU.env
```

3. 启动 CPU worker
```bash
docker compose -f deploy/docker/DOCKER_STACK_WORKER_CPU.compose.yml \
  --env-file deploy/docker/DOCKER_STACK_WORKER_CPU.env up -d --build
```

## 4. frontend（3步）

1. 启动 supabase-lite
```bash
cd /data/V-Bio/frontend/supabase-lite && docker compose up -d
```

2. 准备前端配置
```bash
cd /data/V-Bio/frontend && cp .env.example .env
sed -i "s#^VITE_API_BASE_URL=.*#VITE_API_BASE_URL=http://${HOST_IP}:5000#" .env
sed -i "s#^VITE_API_TOKEN=.*#VITE_API_TOKEN=${API_TOKEN}#" .env
```

3. 启动前端
```bash
npm install && npm run dev
```

## 5. 验证（在中央服务器执行）

```bash
curl -H "X-API-Token: ${API_TOKEN}" "http://${HOST_IP}:5000/workers/capabilities"
curl -H "X-API-Token: ${API_TOKEN}" "http://${HOST_IP}:5000/workers/cluster_status"
```

应看到 `gpu@...` / `cpu@...` 在线，且能力与各自 stack env 声明一致。

## 6. 开更多能力

把 GPU 节点的 `GPU_WORKER_CAPABILITIES` 改成你已安装的组合（例如 `boltz2,alphafold3,protenix,pocketxmol`）。  
如需本地 MSA 服务，可额外启动 `deploy/docker/DOCKER_CAP_COLABFOLD_SERVER.compose.yml`，并在 `deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env`（必要时同步到 worker 的 `DOCKER_STACK_WORKER_*.env`）设置 `MSA_SERVER_URL` 与 `COLABFOLD_JOBS_DIR`。  
详细安装命令见：`docs/deployment/capability-installation.md`。
