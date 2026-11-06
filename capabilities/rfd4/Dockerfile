# RFD4-Proteina 生成引擎镜像：以上游官方镜像为基底，叠加平台封装。
#
# Prerequisite: the upstream image is built first (see the official
# Dockerfile in /data/rfd4-proteina — dependency install and the
# torch/(tmol-ops) combination are verified there). This Dockerfile only
# layers the platform wrapper and examples: no ~7 GB dependency reinstall,
# no proxy sensitivity. The base is a local tag (upstream has no remote
# registry); rebuild upstream before rebuilding this image.
#
#   docker build -t rfd4-proteina-engine:cu13 .
FROM rfd4-proteina:cu13

COPY rfd4_generate.py /app/rfd4_generate.py
COPY examples /app/examples

WORKDIR /workspace
ENTRYPOINT ["python", "/app/rfd4_generate.py"]
