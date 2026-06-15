# V-Bio 前端

前端目录包含 Web UI、management API 和本地 Postgres/PostgREST（supabase-lite）。

## 安装

```bash
cd /data/V-Bio
python3 -m venv venv
source ./venv/bin/activate
pip install -U pip
pip install -r requirements.txt

cd frontend
npm install
cp .env.example .env
```

## 必填配置

修改 `frontend/.env`：

```env
VITE_API_BASE_URL=http://<HOST_IP>:5000
VITE_API_TOKEN=<BOLTZ_API_TOKEN>
VITE_SUPABASE_REST_URL=http://127.0.0.1:54321
VITE_SUPER_ADMIN_USERNAMES=dahuilangda
VITE_SUPER_ADMIN_EMAILS=dahuilangda@hotmail.com
VBIO_JWT_CLIENTS_FILE=frontend/.run/jwt_clients.json
VBIO_SESSION_SECRET=<server-session-secret>
```

## 启动

```bash
cd /data/V-Bio
bash frontend/run.sh start
```

常用命令：

```bash
bash frontend/run.sh status
bash frontend/run.sh stop
bash frontend/run.sh dev
```

默认地址：

```text
Web: http://127.0.0.1:5173
Management API: http://127.0.0.1:5055/vbio-api
PostgREST: http://127.0.0.1:54321
```

## 管理员

创建或升级管理员：

```bash
cd /data/V-Bio/frontend
npm run create-admin -- --username admin --password 'YourPassword' --name 'System Admin' --email admin@example.com
```

用户 CLI：

```bash
npm run users -- list
npm run users -- delete --username alice
npm run users -- set-password --username admin --password 'NewPassword123'
npm run users -- set-admin --username alice --value true
```

超级管理员由 `VITE_SUPER_ADMIN_USERNAMES` 和 `VITE_SUPER_ADMIN_EMAILS` 指定。只有超级管理员能打开用户管理和 Integrations 页面。

## 外部系统登录

超级管理员在 Integrations 页面创建接入方。对接方后端签发短期 JWT 后跳转：

```text
/auth/jwt?token=<JWT>&next=/projects
```

对接文档：`docs/apis/external-system-login.md`。

## API Token 调用

Web 登录后，在项目任务页创建项目 token：

```bash
export VBIO_API_BASE="http://127.0.0.1:5055/vbio-api"
export VBIO_API_TOKEN="<PROJECT_TOKEN>"
export VBIO_PROJECT_ID="<PROJECT_UUID>"

curl -X POST "${VBIO_API_BASE}/predict"   -H "X-API-Token: ${VBIO_API_TOKEN}"   -F "project_id=${VBIO_PROJECT_ID}"   -F "yaml_file=@./config.yaml"   -F "backend=boltz"
```
