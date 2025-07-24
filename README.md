# Boltz-WebUI

**Boltz-WebUI** 是一个为 `boltz-2` 结构预测工具开发的 Web 应用。本项目通过整合 Streamlit 前端、Flask API 以及 Celery 任务队列，将 `boltz-2` 的预测流程封装成一个完整的服务。用户可以通过网页提交预测任务，系统后端会自动处理任务排队、多 GPU 并行计算以及结果的统一管理，目的是帮助使用者更方便地运行预测，并有效利用计算资源。

![Boltz-WebUI Logo](images/Boltz-WebUI-1.png)

## 核心特性 (Features)

  * **🚀 智能任务调度**

      * 内置高/低双优先级队列，自动优先处理来自 Web 界面提交的交互式任务，确保流畅的用户体验。

  * **⚡️ 并行 GPU 集群管理**

      * 自动发现并管理服务器上的所有 GPU 资源，通过并发线程池将计算任务均匀地分配到每一块 GPU 上，实现真正的并行计算。

  * **🔐 全方位 API 安全**

      * 核心 API 端点均受 API 令牌保护，确保只有授权用户和应用才能访问计算资源。

  * **🎨 交互式结果分析**

      * 无需下载，直接在浏览器中渲染可交互的 3D 结构。支持按 pLDDT、链、二级结构等多种方案着色，并可与关键评估指标（pTM, ipTM 等）联动分析。

  * **🖱️ 一键式任务提交**

      * 用户无需关心复杂的命令行参数，只需在网页上填写序列、选择目标，即可一键提交预测任务。

## 安装部署 (Installation)

#### **第 1 步：环境准备**

确保您的服务器满足以下条件：

  * 操作系统：Linux
  * Python 版本：3.9+
  * 硬件：NVIDIA GPU
  * 依赖软件：CUDA Toolkit, Docker

#### **第 2 步：获取代码与安装依赖**

```bash
# 克隆仓库
git clone https://github.com/dahuilangda/Boltz-WebUI.git
cd Boltz-WebUI

# 创建并激活 Python 虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装所有必需的 Python 包
pip install -r requirements.txt

# 赋予启动脚本执行权限
chmod +x run.sh
```

#### **第 3 步：启动 Redis**

使用 Docker 启动 Redis 服务，它将作为 Celery 的消息代理 (Broker)。

```bash
docker run -d -p 6379:6379 --name boltz-webui-redis redis:latest
```

#### **第 4 步：平台配置**

编辑根目录下的 `config.py` 文件：

1.  `RESULTS_BASE_DIR`: 确认结果存储路径存在且有写入权限。
2.  `MAX_CONCURRENT_TASKS`: 根据您的 GPU 数量和显存大小设置最大并发任务数。
3.  `API_SECRET_TOKEN`: 设置一个复杂的安全令牌。**强烈建议**通过环境变量进行配置以提高安全性。

## 使用指南 (Usage)

### **启动平台服务**

您需要打开 **4 个**独立的终端窗口来分别运行平台的不同组件。在**每一个**窗口中都必须能访问到 `API_SECRET_TOKEN` 环境变量。

**首先，设置环境变量 (在每个终端中或在 `.bashrc`/`.zshrc` 中设置):**

```bash
export API_SECRET_TOKEN='your-super-secret-and-long-token'
```

1.  **终端 1 - 初始化 GPU 池** (每次冷启动服务前执行一次):

    ```bash
    bash run.sh init
    ```

2.  **终端 2 - 启动 Celery 计算节点**:

    ```bash
    bash run.sh celery
    ```

3.  **终端 3 - 启动 Flask API 服务器**:

    ```bash
    bash run.sh flask
    ```

4.  **终端 4 - 启动 Streamlit 前端界面**:

    ```bash
    source venv/bin/activate
    streamlit run frontend.py
    ```

服务全部启动后，在浏览器中访问 `http://<您的服务器IP>:8501` 即可开始使用。

### **通过 API 使用 (高级)**

大多数 API 端点都需要在 HTTP 请求头中提供身份验证令牌：`X-API-Token: <您的令牌>`。

#### **提交预测任务**

  * **端点**: `POST /predict`
  * **认证**: 需要 API 令牌
  * **示例**:
    ```bash
    curl -X POST \
         -H "X-API-Token: your-secret-token" \
         -F "yaml_file=@/path/to/your/input.yaml" \
         -F "use_msa_server=true" \
         http://127.0.0.1:5000/predict
    ```

#### **管理任务**

  * **查看任务列表**: `GET /tasks`

      * **认证**: 需要 API 令牌
      * **描述**: 列出所有活跃 (running) 和排队中 (queued/reserved) 的任务。
      * **示例**:
        ```bash
        curl -H "X-API-Token: your-secret-token" http://127.0.0.1:5000/tasks
        ```

  * **终止任务**: `DELETE /tasks/<task_id>`

      * **认证**: 需要 API 令牌
      * **描述**: 终止一个正在运行或在队列中的任务。
      * **示例**:
        ```bash
        curl -X DELETE -H "X-API-Token: your-secret-token" http://127.0.0.1:5000/tasks/some-task-id
        ```

  * **修改任务优先级**: `POST /tasks/<task_id>/move`

      * **认证**: 需要 API 令牌
      * **描述**: 将一个排队中的任务移动到另一个队列。只对尚未开始执行的任务有效。
      * **示例**:
        ```bash
        curl -X POST \
             -H "Content-Type: application/json" \
             -H "X-API-Token: your-secret-token" \
             -d '{"target_queue": "high_priority"}' \
             http://127.0.0.1:5000/tasks/some-task-id-in-queue/move
        ```

#### **MSA 缓存管理**

系统支持 MSA（Multiple Sequence Alignment）智能缓存，为每个蛋白质组分单独缓存 MSA 数据，显著加速重复预测。

  * **获取缓存统计**: `GET /api/msa/cache/stats`

      * **认证**: 需要 API 令牌
      * **描述**: 获取 MSA 缓存的统计信息，包括文件数量、总大小、最早和最新文件时间。
      * **示例**:
        ```bash
        curl -H "X-API-Token: your-secret-token" http://127.0.0.1:5000/api/msa/cache/stats
        ```

  * **智能清理缓存**: `POST /api/msa/cache/cleanup`

      * **认证**: 需要 API 令牌
      * **描述**: 自动清理过期缓存文件（超过7天）和超量缓存文件（超过5GB），返回清理统计。
      * **示例**:
        ```bash
        curl -X POST -H "X-API-Token: your-secret-token" http://127.0.0.1:5000/api/msa/cache/cleanup
        ```

  * **清空全部缓存**: `POST /api/msa/cache/clear`

      * **认证**: 需要 API 令牌
      * **描述**: 清空所有 MSA 缓存文件。谨慎使用！
      * **示例**:
        ```bash
        curl -X POST -H "X-API-Token: your-secret-token" http://127.0.0.1:5000/api/msa/cache/clear
        ```

#### **查询状态与下载结果**

这两个接口是公开的，**无需** `X-API-Token` 即可访问，方便用户和前端轮询。

  * **查询任务状态**: `GET /status/<task_id>`

      * **认证**: 无
      * **描述**: 在下载结果前，您应首先查询任务状态，确保其 `"state"` 值为 `"SUCCESS"`。
      * **示例**:
        ```bash
        curl http://127.0.0.1:5000/status/some-task-id
        ```
      * **返回示例 (成功时):**
        ```json
        {
          "task_id": "some-task-id",
          "state": "SUCCESS",
          "info": {
            "status": "Task completed successfully.",
            "gpu_id": 0,
            "result": { "message": "File uploaded successfully" }
          }
        }
        ```

  * **下载结果文件**: `GET /results/<task_id>`

      * **认证**: 无
      * **描述**: 当任务成功完成后，使用此端点下载包含所有结果（如 .cif, .pdb 文件）的 `.zip` 压缩包。
      * **示例**:
        ```bash
        # 将结果保存为 a_specific_name.zip
        curl -o a_specific_name.zip http://127.0.0.1:5000/results/some-task-id

        # 或者使用 -J -O 让 curl 自动使用服务器提供的文件名 (e.g., some-task-id_results.zip)
        curl -J -O http://127.0.0.1:5000/results/some-task-id
        ```

    如果文件不存在或任务未完成，将返回 404 Not Found 错误。