# RFD4-Proteina 生成引擎镜像：以上游官方镜像为基底，叠加平台封装。
#
# 前提: 上游镜像已构建（见 /data/rfd4-proteina 官方 Dockerfile，依赖安装
# 与 torch/(tmol-ops) 组合已在该步验证）。再构建本 Dockerfile 只叠加
# 平台封装与示例，不重复安装约 7GB 依赖，也不受代理波动影响。
# 基底为本地 tag（上游无远端 registry），重建上游后需同步重建本镜像。
#
#   docker build -t rfd4-proteina-engine:cu13 .
FROM rfd4-proteina:cu13

COPY rfd4_generate.py /app/rfd4_generate.py
COPY examples /app/examples

WORKDIR /workspace
ENTRYPOINT ["python", "/app/rfd4_generate.py"]
