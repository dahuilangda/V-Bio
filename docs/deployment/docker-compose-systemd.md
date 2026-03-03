# Docker Compose + systemd 部署模板

本目录提供官方模板，用于：
- 中央服务器一键起 API/Redis/Monitor
- 计算服务器一键起 GPU 或 CPU worker
- systemd 托管开机自启

## 1. 模板位置

- Runtime 镜像 Dockerfile：`deploy/docker/DOCKER_BACKEND_RUNTIME.Dockerfile`
- Boltz2/Boltz2Score 统一运行镜像 Dockerfile：`deploy/docker/DOCKER_BOLTZ2_RUNTIME.Dockerfile`
- 中央服务器 compose：`deploy/docker/DOCKER_STACK_CENTRAL.compose.yml`
- GPU worker compose：`deploy/docker/DOCKER_STACK_WORKER_GPU.compose.yml`
- CPU worker compose：`deploy/docker/DOCKER_STACK_WORKER_CPU.compose.yml`
- PocketXMol Docker：`deploy/docker/DOCKER_CAP_POCKETXMOL.Dockerfile` / `deploy/docker/DOCKER_CAP_POCKETXMOL.compose.yml`
- ColabFold MSA Docker：`deploy/docker/DOCKER_CAP_COLABFOLD_SERVER.Dockerfile` / `deploy/docker/DOCKER_CAP_COLABFOLD_SERVER.compose.yml`
- MMP PostgreSQL Docker：`deploy/docker/DOCKER_CAP_MMP_POSTGRES.Dockerfile` / `deploy/docker/DOCKER_CAP_MMP_POSTGRES.compose.yml`
- systemd unit 模板：`deploy/systemd/*.service`
- 一键脚本：`deploy/scripts/*.sh`

## 1.1 一键脚本（推荐）

```bash
# 预检查
bash deploy/scripts/preflight.sh all

# 中央服务器
bash deploy/scripts/install_central.sh systemd

# GPU 计算服务器
bash deploy/scripts/install_worker_gpu.sh systemd

# CPU 计算服务器
bash deploy/scripts/install_worker_cpu.sh systemd
```

仅用 compose（不装 systemd）：

```bash
bash deploy/scripts/install_central.sh compose
bash deploy/scripts/install_worker_gpu.sh compose
bash deploy/scripts/install_worker_cpu.sh compose
```

查看集群功能快照：

```bash
bash deploy/scripts/status_cluster.sh http://<CENTRAL_HOST_OR_IP>:5000 <TOKEN>
```

## 2. 中央服务器部署

```bash
cd /data/V-Bio/deploy/docker
cp DOCKER_STACK_CENTRAL.env.example DOCKER_STACK_CENTRAL.env
# 编辑 DOCKER_STACK_CENTRAL.env，至少设置 BOLTZ_API_TOKEN

docker compose -f DOCKER_STACK_CENTRAL.compose.yml --env-file DOCKER_STACK_CENTRAL.env up -d --build
```

验证：

```bash
docker compose -f DOCKER_STACK_CENTRAL.compose.yml --env-file DOCKER_STACK_CENTRAL.env ps
curl -H "X-API-Token: <TOKEN>" http://127.0.0.1:5000/workers/capabilities
```

## 3. GPU 计算服务器部署

先在 GPU 节点构建统一 Boltz 运行镜像：

```bash
cd /data/V-Bio
docker build -f deploy/docker/DOCKER_BOLTZ2_RUNTIME.Dockerfile -t vbio-boltz2-runtime .
```

```bash
cd /data/V-Bio/deploy/docker
cp DOCKER_STACK_WORKER_GPU.env.example DOCKER_STACK_WORKER_GPU.env
# 编辑 DOCKER_STACK_WORKER_GPU.env：CENTRAL 地址、token、功能列表、模型/数据路径

docker compose -f DOCKER_STACK_WORKER_GPU.compose.yml --env-file DOCKER_STACK_WORKER_GPU.env up -d --build
```

关键点：
- 需已安装 NVIDIA Container Toolkit。
- compose 使用 `gpus: all`。
- 挂载 `docker.sock` 以支持 AF3/Protenix/PocketXMol 子容器调度。
- `BOLTZ2_DOCKER_IMAGE` 建议设为 `vbio-boltz2-runtime`，供 boltz2/boltz2score 共用。
- `BOLTZ2_HOST_CACHE_DIR` 建议预置 `boltz2_conf.ckpt`、`boltz2_aff.ckpt`、`ccd.pkl`、`mols.tar`。
- Protenix 官方镜像需准备并挂载 `/data/protenix`（包含 `source/`、`model/`、`common_cache/`）。
- PocketXMol 需在 `capabilities/pocketxmol/weights/` 准备权重包（或已解压权重）。

## 4. CPU 计算服务器部署

```bash
cd /data/V-Bio/deploy/docker
cp DOCKER_STACK_WORKER_CPU.env.example DOCKER_STACK_WORKER_CPU.env
# 编辑 DOCKER_STACK_WORKER_CPU.env：CENTRAL 地址、token、MMP DB 配置

docker compose -f DOCKER_STACK_WORKER_CPU.compose.yml --env-file DOCKER_STACK_WORKER_CPU.env up -d --build
```

## 5. systemd 托管

### 5.1 安装 unit 文件

按角色复制：

```bash
sudo cp /data/V-Bio/deploy/systemd/boltz-central.service /etc/systemd/system/
sudo cp /data/V-Bio/deploy/systemd/boltz-worker-gpu.service /etc/systemd/system/
sudo cp /data/V-Bio/deploy/systemd/boltz-worker-cpu.service /etc/systemd/system/
```

### 5.2 启用与启动

```bash
sudo systemctl daemon-reload
sudo systemctl enable boltz-central.service
sudo systemctl enable boltz-worker-gpu.service
sudo systemctl enable boltz-worker-cpu.service

# 按需启动（每台机器只启对应角色）
sudo systemctl start boltz-central.service
sudo systemctl start boltz-worker-gpu.service
sudo systemctl start boltz-worker-cpu.service
```

### 5.3 查看状态

```bash
sudo systemctl status boltz-central.service
sudo systemctl status boltz-worker-gpu.service
sudo systemctl status boltz-worker-cpu.service
```

## 6. 与调度稳定性相关的固定建议

- 每台 worker 仅声明本机真实功能
- 保持默认可靠性参数：
  - `task_acks_late=True`
  - `task_reject_on_worker_lost=True`
  - `worker_prefetch_multiplier=1`

## 7. 与 V-Bio 前端兼容性说明

这些模板不改对外 API 路径；V-Bio 前端仍对接中央 API。
功能路由与任务队列策略保持后端现状（严格按功能进入队列）。
