# 模型服务

| 服务 | 运行位置 | 关键配置 |
| --- | --- | --- |
| `boltz2` | GPU worker | `BOLTZ2_DOCKER_IMAGE`, `BOLTZ2_HOST_CACHE_DIR` |
| `boltz2score` | GPU worker | `BOLTZ2_DOCKER_IMAGE`, `MSA_SERVER_URL` |
| `affinity` | GPU worker | `BOLTZ2_DOCKER_IMAGE` |
| `alphafold3` | GPU worker | `ALPHAFOLD3_DOCKER_IMAGE`, `ALPHAFOLD3_MODEL_DIR`, `ALPHAFOLD3_DATABASE_DIR` |
| `protenix` | GPU worker | `PROTENIX_DOCKER_IMAGE`, `PROTENIX_SOURCE_DIR`, `PROTENIX_MODEL_DIR` |
| `nesso` | Virtual Screening GPU worker | `NESSO_DOCKER_IMAGE`, `NESSO_HOST_CACHE_DIR`, `NESSO_MODEL_REVISION` |
| `lead_opt` | CPU worker | HALO 生成式先导优化模块（无需外部数据库） |
| ColabFold MSA | 独立服务 | `MSA_SERVER_URL`, `COLABFOLD_JOBS_DIR` |

安装配置见 `docs/deployment/model-services.md`。
