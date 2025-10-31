# ColabFold MSA Server

**ColabFold MSA Server** 提供本地的多序列比对（Multiple Sequence Alignment, MSA）服务。

## 目录 (Table of Contents)

- [安装部署 (Installation)](#安装部署-installation)
- [使用指南 (Usage)](#使用指南-usage)
  - [快速启动](#快速启动)
  - [数据库管理](#数据库管理)
  - [API 使用](#api-使用)
  - [性能调优](#性能调优)
- [配置说明 (Configuration)](#配置说明-configuration)
- [故障排除 (Troubleshooting)](#故障排除-troubleshooting)
- [维护指南 (Maintenance)](#维护指南-maintenance)


## 安装部署 (Installation)

### 前置要求

* **操作系统**: Linux (推荐 Ubuntu 20.04+)
* **容器运行时**: Docker 20.0+, Docker Compose 2.0+
* **存储空间**: 至少 500GB 可用空间（用于数据库）
* **内存**: 至少 16GB RAM（推荐 32GB+）
* **网络**: 稳定的网络连接（用于数据库下载）

### 快速安装

#### 第 1 步：下载项目代码

```bash
# 如果尚未克隆主项目
git clone https://github.com/dahuilangda/Boltz-WebUI.git
cd Boltz-WebUI/colabfold_server
```

#### 第 2 步：准备数据库

**注意**: 首次运行需要下载约 200-300GB 的数据库文件，请确保有足够的存储空间和时间。

```bash
# 运行数据库准备脚本（两种方式指定路径）：

# 方式1: 通过命令行参数指定数据库目录
chmod +x prepare_databases.sh
./prepare_databases.sh /path/to/your/databases

# 方式2: 通过环境变量指定（可选）
export DB_DIR="/path/to/your/databases"
./prepare_databases.sh

# 方式3: 使用默认路径 /home/dahuilangda/DATABASE
./prepare_databases.sh
```

#### 第 3 步：构建和启动服务

```bash
# 方式1: 使用默认数据库路径 ./databases
docker compose up -d --build

# 方式2: 指定自定义数据库路径（环境变量）
export DB_DIR="/path/to/your/databases"
docker compose up -d --build

# 方式3: 使用 .env 配置文件（推荐）
cp .env.docker .env
# 编辑 .env 文件，设置 DB_DIR=/path/to/your/databases
docker compose up -d --build

# 查看服务状态
docker compose ps

# 查看日志
docker compose logs -f colabfold-api
```

## 使用指南 (Usage)

### 快速启动

#### 启动服务

```bash
# 前台运行（用于调试）
docker compose up

# 后台运行（生产环境）
docker compose up -d
```

#### 停止服务

```bash
# 停止服务
docker compose down

# 停止服务并删除卷（谨慎使用）
docker compose down -v
```

#### 重启服务

```bash
# 重启所有服务
docker compose restart

# 重启特定服务
docker compose restart colabfold-api
```

### 数据库管理

#### 手动数据库更新

```bash
# 进入容器
docker compose exec colabfold-api bash

# 在容器内运行数据库更新脚本
./start.sh
```

#### 数据库状态检查

```bash
# 检查数据库文件完整性
ls -la databases/
du -sh databases/*

# 检查数据库索引状态
docker-compose exec colabfold-api mmseqs databases
```

### API 使用

#### 基本 API 端点

服务默认运行在 `http://localhost:8080`

**提交 MSA 搜索任务**:
```bash
curl -X POST \
     -d 'q=>query\nMKFLILLFNILCLF...' \
     -d 'mode=colabfold' \
     http://localhost:8080/ticket/msa
```

**查询任务状态**:
```bash
curl http://localhost:8080/ticket/{ticket_id}
```

**下载结果**:
```bash
curl http://localhost:8080/result/download/{ticket_id}
```

#### 集成到 Boltz-WebUI

MSA 服务器已经集成到主 Boltz-WebUI 系统中。在 `config.py` 中配置：

```python
# 配置 MSA 服务器地址
MSA_SERVER_URL = "http://localhost:8080"  # 本地部署
# 或
MSA_SERVER_URL = "https://api.colabfold.com"  # 使用 ColabFold 公共服务
```

### 性能调优

#### 调整工作进程数

编辑 `config.json`:
```json
{
  "local": {
    "workers": 8  // 根据 CPU 核心数调整
  }
}
```

#### 内存和存储优化

编辑 `docker-compose.yml`:
```yaml
services:
  colabfold-api:
    mem_limit: '64g'  # 根据可用内存调整
    shm_size: '32gb'  # 共享内存大小
```

## 配置说明 (Configuration)

### 主要配置文件

#### config.json

主要的服务器配置文件：

```json
{
    "app": "colabfold",
    "verbose": true,
    "server": {
        "address": "0.0.0.0:8080",
        "dbmanagment": false,
        "cors": true
    },
    "paths": {
        "databases": "/app/databases",
        "results": "/app/jobs",
        "temporary": "/app/tmp",
        "colabfold": {
            "parallelstages": true,
            "uniref": "/app/databases/uniref30_2302_db",
            "pdb": "/app/databases/pdb100_230517",
            "environmental": "/app/databases/colabfold_envdb_202108_db",
            "pdb70": "/app/databases/pdb100_230517"
        }
    },
    "local": {
        "workers": 4
    }
}
```

#### docker-compose.yml

Docker 服务配置：

```yaml
services:
  colabfold-api:
    build: .
    container_name: colabfold_api_server
    restart: on-failure:5
    ports:
      - "8080:8080"
    volumes:
      - ${DB_DIR:-./databases}:/app/databases  # 支持通过 DB_DIR 环境变量自定义数据库路径
      - ./jobs:/app/jobs
      - ./config.json:/app/config.json:ro
    shm_size: '16gb'
    mem_limit: '32g'
```

### 环境变量配置

Docker Compose 支持通过环境变量或 `.env` 文件配置以下参数：

#### Docker Compose 环境变量

| 变量名 | 描述 | 默认值 | 用途 |
|--------|------|---------|------|
| `DB_DIR` | 宿主机数据库存储路径 | `./databases` | 卷挂载宿主机数据库目录到容器内 |
| `MMSEQS_LOAD_MODE` | MMseqs2 数据库加载模式（`0`=流式、`2`=内存加载） | `2` | 控制 GPU/CPU 内存占用与性能权衡 |
| `HTTP_PROXY` | Docker 构建及运行时 HTTP 代理 | 空 | 通过代理访问外部资源 |
| `HTTPS_PROXY` | Docker 构建及运行时 HTTPS 代理 | 空 | 同上 |
| `NO_PROXY` | 代理豁免列表 | 空 | 指定无需代理的主机/网段 |
| `UID` / `GID` | 容器运行时使用的宿主机用户/用户组 ID | `1000` | 以非 root 身份运行，避免生成 root 拥有的文件 |

#### 容器内环境变量

| 变量名 | 描述 | 默认值 | 用途 |
|--------|------|---------|------|
| `PDB_SERVER` | PDB 同步服务器 | `rsync.wwpdb.org::ftp` | 容器内数据库同步 |
| `PDB_PORT` | PDB 服务器端口 | `33444` | 容器内数据库同步 |
| `GPU` | 启用 GPU 支持 | 空（禁用） | 容器内 MMseqs2 加速 |
| `MMSEQS_LOAD_MODE` | 同上 | 继承自 `.env` | 传递给 MMseqs2 控制加载模式 |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | 构建时传入的代理设置 | 继承自 `.env` | 支撑容器内外一致的网络代理 |
| `UID` / `GID` | 运行时传入的 UID/GID | 继承自 `.env` 或宿主机环境 | 控制容器进程的权限 |

> 提示：Dockerfile 会在构建阶段自动读取 `.env` 中的 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`，若留空则不会启用代理。

**重要说明**:
- `DB_DIR` 是 **Docker Compose** 级别的环境变量，用于卷挂载配置
- 其他变量是容器内的环境变量，用于容器运行时配置
- **强烈建议**在生产环境中，先在容器外准备好数据库，然后通过 `DB_DIR` 挂载到容器中
- 若使用 `UID/GID` 让容器以当前用户运行，确保宿主机上的 `jobs/`、`databases/` 目录对该用户有写权限；必要时执行 `sudo chown -R $(id -u):$(id -g) jobs databases`

## 故障排除 (Troubleshooting)

### 常见问题

#### 1. 服务无法启动

**症状**: 容器启动失败或立即退出

**解决方案**:
```bash
# 检查日志
docker-compose logs colabfold-api

# 检查配置文件语法
cat config.json | python -m json.tool

# 检查端口占用
netstat -tulpn | grep 8080
```

#### 2. 数据库下载失败

**症状**: 数据库准备脚本报错

**解决方案**:
```bash
# 检查网络连接
ping ftp.uniprot.org

# 检查磁盘空间
df -h

# 手动重试下载
./prepare_databases.sh
```

#### 3. MSA 搜索超时

**症状**: API 请求超时或返回错误

**解决方案**:
```bash
# 检查服务状态
docker-compose ps

# 增加超时设置
# 在 config.json 中调整超时参数

# 重启服务
docker-compose restart
```

#### 4. 内存不足

**症状**: 容器因内存不足被杀死

**解决方案**:
```bash
# 增加内存限制
# 编辑 docker-compose.yml 中的 mem_limit

# 减少并发工作进程
# 编辑 config.json 中的 workers 数量

# 监控内存使用
docker stats colabfold_api_server
```

### 日志分析

#### 查看详细日志

```bash
# 实时日志
docker-compose logs -f colabfold-api

# 最近的日志
docker-compose logs --tail=100 colabfold-api

# 保存日志到文件
docker-compose logs colabfold-api > msa_server.log
```

#### 调试模式

```bash
# 使用调试启动脚本
docker-compose exec colabfold-api ./start_debug.sh

# 进入容器进行手动调试
docker-compose exec colabfold-api bash
```

## 维护指南 (Maintenance)

### 定期维护任务

#### 数据库更新（每月）

```bash
# 停止服务
docker-compose down

# 更新数据库
./prepare_databases.sh

# 重新启动服务
docker-compose up -d
```

#### 清理临时文件（每周）

```bash
# 清理作业目录
docker-compose exec colabfold-api find /app/jobs -type f -mtime +7 -delete

# 清理临时目录
docker-compose exec colabfold-api rm -rf /app/tmp/*
```
