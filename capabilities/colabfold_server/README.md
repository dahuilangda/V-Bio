# ColabFold MSA Server

该目录保留 ColabFold MSA 服务器的运行脚本与配置文件。
统一 Docker 安装入口在 `deploy/docker/DOCKER_CAP_COLABFOLD_SERVER.*`。

## 1. 启动

```bash
cd /data/V-Bio/deploy/docker
cp DOCKER_CAP_COLABFOLD_SERVER.env.example DOCKER_CAP_COLABFOLD_SERVER.env
# 编辑 DOCKER_CAP_COLABFOLD_SERVER.env
# 国内网络可额外设置:
# APT_MIRROR=https://mirrors.aliyun.com/ubuntu
# GO_DOWNLOAD_URL=https://mirrors.aliyun.com/golang
# GO_MODULE_PROXY=https://goproxy.cn,direct
# GO_SUMDB=off
# MMSEQS_DOWNLOAD_URL=https://mmseqs.com/latest/mmseqs-linux-gpu.tar.gz
# COLABFOLD_ENABLE_GPU=1
# COLABFOLD_DOCKER_GPUS=all
docker compose -f DOCKER_CAP_COLABFOLD_SERVER.compose.yml \
  --env-file DOCKER_CAP_COLABFOLD_SERVER.env up -d --build
```

GPU 模式说明：
- 当前只保留单一 MMseqs 二进制（建议 `mmseqs-linux-gpu`）。
- `COLABFOLD_ENABLE_GPU=1` 时会在数据库处理阶段启用 MMseqs GPU 参数（如 `--gpu 1`）。
- 如 Docker 默认 runtime 不是 `nvidia`，请确保容器有 GPU 设备访问权限（例如 compose 服务增加 `gpus: all`）。

## 2. 后端对接

在中央/worker 对应 stack env 中设置（例如 `deploy/docker/DOCKER_STACK_CENTRAL_DECOUPLED.env`）：

```env
MSA_SERVER_URL=http://<MSA_HOST>:8080
MSA_SERVER_MODE=colabfold
COLABFOLD_JOBS_DIR=/data/colabfold/jobs
```

`COLABFOLD_JOBS_DIR` 需与 `DOCKER_CAP_COLABFOLD_SERVER.env` 的 `COLABFOLD_JOBS_DIR` 保持一致。

## 3. 常用命令

```bash
cd /data/V-Bio/deploy/docker
docker compose -f DOCKER_CAP_COLABFOLD_SERVER.compose.yml \
  --env-file DOCKER_CAP_COLABFOLD_SERVER.env ps

docker compose -f DOCKER_CAP_COLABFOLD_SERVER.compose.yml \
  --env-file DOCKER_CAP_COLABFOLD_SERVER.env logs -f colabfold-server
```

## 4. 数据库准备

- 可在容器内运行 `/app/prepare_databases.sh` 下载并构建索引。
- `COLABFOLD_DB_DIR` 需要预留充足存储空间（数据库体积较大）。
- 默认配置文件为本目录下 `config.json`，可通过 `COLABFOLD_CONFIG_PATH` 覆盖。

## 5. API 验证

```bash
curl -X POST \
  -d 'q=>query\nMKFLILLFNILCLF...' \
  -d 'mode=colabfold' \
  http://127.0.0.1:8080/ticket/msa
```
