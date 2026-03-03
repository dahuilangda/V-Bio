# Deployment 文档目录

本目录用于部署与运维，文件命名统一为 `kebab-case`。

- [quick-start.md](./quick-start.md)：首次部署推荐，一页跑通中央/GPU/CPU/frontend。
- [single-node-all-apps.md](./single-node-all-apps.md)：单机微服务全量安装（中央/Redis/MSA/MMP/GPU/CPU/frontend）。
- [single-node-10-commands.md](./single-node-10-commands.md)：单机快速试跑，10条命令完成。
- [capability-installation.md](./capability-installation.md)：各能力（Boltz2/AF3/Protenix/PocketXMol/ColabFold/Lead Opt）安装与配置。
- [docker-compose-systemd.md](./docker-compose-systemd.md)：Compose 与 systemd 模板化部署。
- [microservice-decoupling.md](./microservice-decoupling.md)：中央/Redis/Capability/MSA 解耦部署。
- [containerization-roadmap.md](./containerization-roadmap.md)：容器化演进规划与边界说明。

当前推荐仅使用：
- [`deploy/docker/README.md`](../../deploy/docker/README.md)：统一 `DOCKER_*` Docker 安装入口与 env 样例。
