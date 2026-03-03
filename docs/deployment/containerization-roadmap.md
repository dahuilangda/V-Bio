# 全功能 Docker 化蓝图

目标：降低计算服务器装机复杂度，做到“拉镜像 + 配置 `.env` + 启动 worker”。

## 1. 当前状态

- 已 Docker 执行：`alphafold3`、`protenix`、`pocketxmol`
- 尚未完全 Docker 执行：`boltz2`、`lead_opt(MMP)` 主执行链

## 2. 推荐目标状态

四大基础功能都采用容器化执行：
- `boltz2`: 运行时容器化（worker 内或子进程 docker run）
- `alphafold3`: 维持容器化
- `protenix`: 当前采用官方镜像 + 宿主机挂载源码/权重/common
- `pocketxmol`: 推荐提供统一预构建镜像与权重层

`lead_opt(MMP)` 建议拆分：
- PostgreSQL + mmpdb 工具链容器化
- CPU worker 仍可宿主执行，也可后续容器化

## 3. Protenix 现状

Protenix 当前为官方镜像兼容模式：
- 挂载输入/输出/common_cache
- 额外挂载宿主机 `PROTENIX_SOURCE_DIR`（源码）与 `PROTENIX_MODEL_DIR`（权重）

后续若维护自定义镜像，可再评估完全内置 runner/checkpoint 的简化方案。

## 4. PocketXMol 改造建议

建议从“仓库内 compose + 本地文件”升级为：
- 固定版本预构建镜像（含运行依赖）
- 权重层可做成独立镜像层或启动时一次性拉取到持久卷
- 保留 `POCKETXMOL_*` `.env` 参数用于业务可调项（batch/device/config）

## 5. Boltz2 Docker 化建议

推荐两步走：
1. 短期：将 GPU worker 进程整体容器化（不改任务代码路径）。
2. 中期：按功能改为 `docker run` 子进程执行（与 AF3/Protenix 统一模型）。

## 6. 对调度稳定性的要求

无论功能是否 Docker 化，都保持：
- capability 队列路由
- `worker_prefetch_multiplier=1`
- `task_acks_late=True`
- `task_reject_on_worker_lost=True`
- 无在线功能 worker 时返回 `503`（不会进入默认队列）

## 7. 建议落地顺序

1. Protenix 官方镜像模式稳定运行并固化目录规范。
2. 再统一 PocketXMol 预构建镜像。
3. 最后推进 Boltz2 与 MMP 的容器化执行链。
