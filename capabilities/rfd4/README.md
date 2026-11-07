# RFD4-Proteina generation engine

RFD4-Proteina (Baker Lab × NVIDIA, BSD-3) is an all-atom flow-matching
generative model with co-design of sequence and structure. This directory
is the V-Bio engine wrapper: image build, the generation wrapper
(`rfd4_generate.py`), deployment smoke, and validated examples.

## Capability matrix (verified on RTX 4090)

| Requirement | Supported | Notes |
| --- | --- | --- |
| Linear peptides | Yes | binders and short peptides are core tasks; sequence-structure co-design |
| Non-canonical amino acids | Yes | vocabulary natively covers 100 NCAA types (AIB/NLE/ORN/CIT/HYP/SEC/CSO and other common pharma ones), plus atomized tokens for arbitrary chemistry |
| Cyclic peptides | Via motif conditioning | no first-class cyclic condition; fix cyclization bonds / terminal Cys as annotated-CIF components and let the model scaffold the rest (verified) |
| Bicyclic | Via motif conditioning | SEZ three-arm + three Cys anchors pinned by conditions; verified output closes the trithioether (1.98/2.25/2.18 Å) with new ring sequence `CPTAAAICAALGIPC` |

NCAA 生成的实测样例见 `examples/ncaa_phosphopeptide.yaml`：PTR（磷酸酪氨酸）
身份与磷酸基坐标固定后，输出结构完整保留 PTR 残基与磷酸基坐标。

## Requirements

- Linux x86_64, NVIDIA driver compatible with CUDA 12 or 13 (host runs 580.178.04)
- 24 GB VRAM (verified on RTX 4090; official tested list is L40S / RTX 6000 Ada / A100 / H100 class)
- Disk: ~8.7 GB image, ~1.7 GB HF weights, plus compile cache
- Network: build and first run need proxy access to PyPI / PyTorch index / Hugging Face

## Image build

The engine image layers on top of the upstream official image (build
upstream first, then this wrapper; both steps are offline-repeatable):

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

The dependency set comes from the upstream `pyproject.toml` pinned in this
directory (version combination verified by testing). If the driver only
supports CUDA 12, change the upstream build-arg to `cu12`. The smoke script's
`IMAGE=` only accepts the wrapper image `rfd4-proteina-engine:cu13` (the
entry point is bound to `/app/rfd4_generate.py`; the upstream image has no
/app).

## Running

The wrapper entry point is `/app/rfd4_generate.py` inside the container:

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--spec` | required | condition spec YAML/JSON, relative to container `/workspace` |
| `--out-dir` | required | output directory, writes structure CIFs and `summary.json` |
| `--num-designs` | 2 | number of designs |
| `--seed` | 0 | random seed; defaulted, pass explicitly to pin reproducibility |
| `--preset` | quickstart | inference preset; quickstart skips scoring and compile warm-up |
| `--timeout-min` | 120 | generation timeout in minutes; hard error on exceed |
| `--allow-cpu` | off | diagnostic run without a GPU |
| `--extra` | none | extra Hydra override, repeatable (e.g. `--extra nsteps=80`) |

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

Structures land in `runs/shortpep/inference_samples/*/`; `summary.json`
records per-chain residue counts and one-letter sequences (non-standard
residues reported as `[CCD_NAME]`).

## GPU 使用规范

- 单卡单任务：一个容器只绑一张卡（`--gpus device=N`），多任务由宿主侧排队，
  不在容器内做多卡分发。
- 封装启动时断言 CUDA 可用并记录设备名与显存；无卡时显式报错，
  `--allow-cpu` 仅用于诊断。
- `--seed` 有默认值；显式指定同 seed 同条件可复现。
- 长任务必须带超时（宿主侧 `timeout` 或 `--timeout-min`），避免挂死占卡。

## Deployment smoke

```bash
GPU=0 bash capabilities/rfd4/scripts/run_smoke.sh
```

Generates the four cases sequentially and asserts artifact counts; any case
failing fails the whole run. The first run pulls weights via
`HF_ENDPOINT=https://hf-mirror.com` (~1.7 GB; measured an order of magnitude
faster than the main site here), later runs reuse the cache.

## Unit tests

```bash
docker run --rm -v "$PWD:/app" -w /app \
  rfd4-proteina-engine:cu13 bash -c \
  'pip install -q pytest && python -m pytest tests -q'
```

In-container pip needs network; inject proxy env vars
(`-e HTTPS_PROXY=... -e http_proxy=...`) when offline.

## Known issues and boundaries

- Length windows are soft for the proposer: `LengthMin/Max` do not pin the
  decoded length (a 15 window produced a 13-mer in testing). The combination
  of a variable-length window with `peptideFixedModifications` crashes on
  anchor misalignment (staged structure is built at the window cap) — use a
  single-length window (`LengthMin=LengthMax`) when fixed modifications are
  required.
- Residue-pool restrictions (excluding canonicals, NCAA presets only) do not
  constrain the proposer: free evolution collapses back to canonicals. To
  actually incorporate NCaas, use `peptideFixedModifications` fixed positions,
  or the Mol-1 maturation route (single-point scan -> combination check, see
  the FadA campaign's K9->ORN case).
- The platform BS3 CCD is a Bi single-atom placeholder, not synthesizable;
  bicyclic linkers must use SEZ.
- The platform-side residue whitelist now covers the designer's NCAA presets
  (`protenix2dock/core/structure.py`); without it, fixed-modification
  candidates crash in refine when those residues get dropped and anchors
  misalign.

## Platform pipeline hand-off

Generated structures (mmCIF) feed directly into the existing verification
pipeline: protenix2dock re-poses and refines against the pocket, Mol-1 runs
independent co-folding validation, and candidates enter the shortlist only
when both agree. The full FadA campaign practice is in
`/data/fada_sez_campaign.zip` (`final_list.txt` and validation records).

### Verification dispatch (deep fusion with V-Bio engines)

RFD4 deliberately ships no oracle of its own in this integration. Every
design is verified by the engines V-Bio already operates — Mol-1, Protenix,
AlphaFold3 — through the platform gateway, then scored with the shared
evaluator (ipTM/ipSAE, LRMSD, iRMSD, contact F1, DockQ, functional-surface
overlap).

```bash
python3 scripts/verify_designs.py \
  --runs-dir /data/rfd4-proteina/runs/<job> \
  --target-sequence "<receptor seq>" \
  --token <gateway token> --project-id <project> \
  --backends mol1,protenix --top 5 --seed 101
```

Each generated binder is submitted as a co-folding prediction to every
backend in `--backends`; the manifest lands next to the run as
`verification_manifest.json`. Scores are compared once the tasks finish
(poll `/vbio-api/status/<task_id>`), using the campaign evaluator — a design
enters the shortlist only when engines agree on the binding site and the
functional-surface overlap clears.
