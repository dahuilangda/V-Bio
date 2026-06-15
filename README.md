# V-Bio

V-Bio 是结构预测和药物发现计算任务平台，包含 Web 前端、管理 API、中心 API、调度服务和 CPU/GPU worker。

## 目录结构

```text
backend/        中心 API、调度、worker 运行时代码
frontend/       Web 前端、management API、supabase-lite
deploy/docker/  Docker Compose 和镜像配置
docs/           部署、配置、模型服务和接口文档
capabilities/   模型服务代码
```

## 快速启动

后端和 worker 使用 Docker 部署：

```bash
cd /data/V-Bio
# 先按实际地址和 token 修改 deploy/docker/*.env
docker compose -f deploy/docker/DOCKER_STACK_REDIS.compose.yml   --env-file deploy/docker/DOCKER_STACK_REDIS.env up -d
docker compose -f deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.compose.yml   --env-file deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env up -d --build
```

前端和 management API：

```bash
cd /data/V-Bio
bash frontend/run.sh start
```

完整步骤见 `docs/deployment/quick-start.md`。

## 基础配置

前端配置文件是 `frontend/.env`。最少需要确认：

```env
VITE_API_BASE_URL=http://<HOST_IP>:5000
VITE_API_TOKEN=<BOLTZ_API_TOKEN>
VITE_SUPER_ADMIN_USERNAMES=dahuilangda
VITE_SUPER_ADMIN_EMAILS=dahuilangda@hotmail.com
VBIO_JWT_CLIENTS_FILE=frontend/.run/jwt_clients.json
VBIO_SESSION_SECRET=<server-session-secret>
```

## 文档

- `docs/deployment/quick-start.md`：单机部署
- `docs/deployment/multi-node.md`：多节点部署
- `docs/deployment/model-services.md`：模型服务安装
- `docs/configuration/env.md`：环境变量
- `docs/apis/external-system-login.md`：外部系统登录
- `docs/apis/worker-services.md`：worker 服务接口
- `deploy/docker/README.md`：Docker 文件索引

## 验证

```bash
curl -H "X-API-Token: <TOKEN>" http://<HOST_IP>:5000/workers/capabilities
curl -H "X-API-Token: <TOKEN>" http://<HOST_IP>:5000/workers/cluster_status
```
