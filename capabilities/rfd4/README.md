# RFD4-Proteina 多肽/蛋白生成引擎

RFD4-Proteina（Baker Lab × NVIDIA，BSD-3）是全原子 flow-matching 生成模型，
序列与结构共设计。本目录是它在 V-Bio 里的引擎封装：镜像构建、生成封装
（`rfd4_generate.py`）、部署烟测与实测样例。

## 功能边界（RTX 4090 实测）

| 需求 | 支持 | 说明 |
| --- | --- | --- |
| 线性多肽 | 支持 | binder 与短肽均为核心任务，序列-结构共设计 |
| 非天然氨基酸 | 支持 | 词表原生 100 类 NCAA（AIB/NLE/ORN/CIT/HYP/SEC/CSO 等成药常用全在），另有 atomized token 表达任意化学组件 |
| 环肽 | 经 motif 条件化 | 无一等 cyclic 条件；把环化键/首尾 Cys 作为 annotated CIF 固定组件，模型 scaffold 其余序列（实测可行） |
| 双环 | 经 motif 条件化 | SEZ 三臂 + 三 Cys 锚点条件化钉定，实测输出三硫醚闭合（1.98/2.25/2.18 Å），新环序列 `CPTAAAICAALGIPC` |

NCAA 生成的实测样例见 `examples/ncaa_phosphopeptide.yaml`：PTR（磷酸酪氨酸）
身份与磷酸基坐标固定后，输出结构完整保留 PTR 残基与磷酸基坐标。

## 系统要求

- Linux x86_64，NVIDIA 驱动兼容 CUDA 12 或 13（本机 580.178.04 满足）
- 显存 24 GB（RTX 4090 实测通过；官方 tested 列表为 L40S/RTX 6000 Ada/A100/H100 级）
- 磁盘：镜像约 8.7 GB，HF 权重约 1.7 GB，另需编译缓存若干 GB
- 网络：构建与首次运行需代理可达 PyPI / PyTorch 索引 / Hugging Face

## 镜像构建

引擎镜像以上游官方镜像为基底（先构建上游，再叠加封装，两步均离线可重复）：

```bash
# 1) 上游镜像：依赖安装与验证（需代理）
git clone --depth 1 https://github.com/RosettaCommons/RFD4-Proteina.git
cd RFD4-Proteina
docker build --build-arg HTTP_PROXY=http://<proxy>:2080 \
  --build-arg HTTPS_PROXY=http://<proxy>:2080 \
  --build-arg RFD4_ACCELERATOR=cu13 -t rfd4-proteina:cu13 .

# 2) 平台封装镜像：纯 COPY，秒级
cd ../V-Bio/capabilities/rfd4
docker build -t rfd4-proteina-engine:cu13 .
```

依赖清单来自随目录钉死的上游 `pyproject.toml`（版本组合经实测可复现）。
驱动只支持 CUDA 12 时上游 build-arg 换成 `cu12`。烟测脚本
`IMAGE=` 只认封装镜像 `rfd4-proteina-engine:cu13`（入口路径绑定
`/app/rfd4_generate.py`，上游镜像无 /app）。

## 运行方式

封装入口为容器内 `/app/rfd4_generate.py`，参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--spec` | 必填 | 条件化 YAML/JSON，路径相对容器 `/workspace` |
| `--out-dir` | 必填 | 输出目录，写结构 cif 与 `summary.json` |
| `--num-designs` | 2 | 重复设计数 |
| `--seed` | 0 | 随机种子；有默认值，显式指定以固定复现 |
| `--preset` | quickstart | 推理预设；quickstart 跳过评分与编译预热 |
| `--timeout-min` | 120 | 生成超时（分钟），超时显式报错 |
| `--allow-cpu` | 关 | 允许无 GPU 的诊断运行 |
| `--extra` | 无 | 追加 Hydra override，可多次（如 `--extra nsteps=80`）|

四个实测用例在 `examples/`，各自的靶点输入在 `examples/inputs/`：

```bash
docker run --rm --runtime nvidia --gpus device=0 \
  -e HF_HOME=/workspace/hf_cache \
  -v /data/rfd4-proteina/hf_cache:/workspace/hf_cache \
  -v /data/rfd4-proteina/runs:/workspace/runs \
  -v "$PWD/examples:/workspace/examples:ro" \
  -w /workspace \
  --entrypoint python3 \
  rfd4-proteina-engine:cu13 /app/rfd4_generate.py \
    --spec examples/shortpep.yaml --out-dir runs/shortpep \
    --num-designs 2 --seed 101
```

生成后结构在 `runs/shortpep/inference_samples/*/`，`summary.json` 记录每条
链的残基数与 one-letter 序列（非标准残基记作 `[CCD名]`）。

## GPU 使用规范

- 单卡单任务：一个容器只绑一张卡（`--gpus device=N`），多任务由宿主侧排队，
  不在容器内做多卡分发。
- 封装启动时断言 CUDA 可用并记录设备名与显存；无卡时显式报错，
  `--allow-cpu` 仅用于诊断。
- `--seed` 有默认值；显式指定同 seed 同条件可复现。
- 长任务必须带超时（宿主侧 `timeout` 或 `--timeout-min`），避免挂死占卡。

## 部署烟测

```bash
GPU=0 bash capabilities/rfd4/scripts/run_smoke.sh
```

依次生成四个用例并断言产物数量，任一用例失败整体失败。
首次运行会经 `HF_ENDPOINT=https://hf-mirror.com` 拉取权重（约 1.7 GB，
本机实测镜像源比主站快一个量级），之后不再下载。

## 单元测试

```bash
docker run --rm -v "$PWD:/app" -w /app \
  rfd4-proteina-engine:cu13 bash -c \
  'pip install -q pytest && python -m pytest tests -q'
```

容器内 pip 需网络，离线环境给容器注入代理 env
（`-e HTTPS_PROXY=... -e http_proxy=...`）。

## 已知问题与边界

- 提案器对长度窗口是软控：`LengthMin/Max` 压不住解码长度（实测 15 窗口出
  13 聚体）。可变长窗口 + `peptideFixedModifications` 组合会因 staged 结构
  （按窗口上限构建）与提案序列的锚点错位而崩溃——需要固定修饰时用
  单长度窗口（`LengthMin=LengthMax`）。
- 池限制（剔除天然残基、只留 NCAA 预设）约束不住提案器：自由进化仍收敛回
  天然序列。要真正掺入 NCAA，用 `peptideFixedModifications` 固定位，或走
  Mol-1 成熟化路线（单点扫描 → 组合验证，见 FadA 战役的 K9→ORN 案例）。
- 本平台 BS3 CCD 是 Bi 单原子占位符，不可合成；双环连接子一律用 SEZ。
- 平台侧残基白名单已补全设计器 NCAA 预设（`protenix2dock/core/structure.py`），
  否则固定修饰候选会在 refine 阶段因残基被丢弃而锚点错位崩溃。

## 与平台管线的衔接

生成产物（mmCIF）可直接进入既有验证管线：protenix2dock 按 pocket 重pose
refine，Mol-1 做独立共折叠验证，两侧一致才入候选名单。FadA 战役的完整
实践见 `/data/fada_sez_campaign.zip` 内 `final_list.txt` 与验证记录。
