# protenix2dock

Six-mode protein–ligand workflow on the **Protenix** engine, mirroring
Boltz2Score's mode semantics so the two engines can be compared benchmark-for-benchmark.

| Mode | What it does | Schedule (sigma_max / steps / samples) |
| --- | --- | --- |
| `score` | Confidence scoring of an input pose — diffusion bypassed, confidence heads evaluate the input coordinates directly | — / — / 1 |
| `pose` | Refinement keeping the input pose close | 0.02 / 8 / 5 |
| `refine` | General flexible refinement | 0.03 / 10 / 5 |
| `interface` | Interface-focused refinement | 0.04 / 12 / 5 |
| `dock` | SMILES → pocket-centred placement ensemble (one rotation per diffusion sample) against a rigid receptor (pinned to the input structure), with steric-floor hard anchors and contact-guided sampling (calibrated small-sigma ladder; the TFG guidance projection is only well-conditioned in the local refinement regime) | 0.05 / 12 / 5 |
| `peptide` | Receptor-fixed peptide inpainting — peptide as a proteinChain, receptor pinned every step, covalent-bond TFG for bicyclics, mirror-space D-peptide design | 0.05 / 12 / 8 |

Protenix's `InferenceNoiseScheduler` shares Boltz2's EDM parameterisation
(`sigma_data = 16`), so `s_max == sigma_max` reproduces Boltz2Score's schedule
range exactly (first-step noise = `16 × s_max` Å).

## Interface metrics (`--interface_chains`)

Reported interface metrics are scoped to the chains the caller declares, not
the all-pairs global: `--interface_chains 'A,B'` (receptor vs peptide/ligand)
or `'AB,C'` (multi-chain receptor vs ligand). ipSAE runs against the second
group's ligand chain, `pair_iptm` is the weakest cross-group pair from the
engine's chain-pair matrix, and `ligand_plddt` is the ligand chain's own
plddt. The global iptm averages over every chain pair — a bicyclic linker
pair (0.09) or a homodimer interface dominates it and misreports the
interface the user asked about.

`--blind` (dock mode) runs the engine's standard full-noise diffusion: no
pocket box, no placement ensemble, no receptor pin, no TFG/anchors —
sequence + SMILES in, complex out.

## Native affinity head (从零训练，深度融合)

`ProtenixAffinityHead` (`vendor/.../protenix/model/modules/affinity.py`) is a
3.3M-parameter head **fused into the Protenix forward pass** — not a
post-hoc pipeline stage:

- **Same call site as the confidence head**: consumes the trunk's
  `s_inputs`/`z` representations plus the (predicted or input) coordinates,
  inside `_main_inference_loop`; per-sample outputs merge straight into
  `pred_dict["summary_confidence"]`, so the stock dumper writes them with
  zero layout changes (`affinity_pred_value`, `..._value1/2`,
  `..._probability_binary`, `..._pred_score`, `..._pred_std`).
- **Explicit 3D evidence** (boltz2 精华): minimal atom-distance per interface
  token pair, binned + embedded, injected into z before interface-focused
  pair attention (lig-rec + rec-lig + lig-lig pairs only).
- **Generalisation** (nesso-1 精华): MC-dropout gives `affinity_pred_std`;
  a two-path value ensemble; training randomises `use_msa` per sample
  (`--msa_prob 0.5`) so the head works MSA-free at inference.
- **Strict fp32** compute (autocast disabled) — required under
  `PROTENIX_LOW_VRAM`'s bf16 trunk.
- Loaded via the `PROTENIX_AFFINITY_CKPT` env side channel (or CLI
  `--affinity_head_ckpt`); construction is deferred until after the stock
  checkpoint loads, so the main weights stay untouched.

### Module cache (冷启动加速)

Protenix model **construction** costs ~80 s per task (every layer is built
with random weights that `load_checkpoint` immediately overwrites — the same
waste Boltz2Score's whole-module cache eliminates).  When
`PROTENIX_MODULE_CACHE_DIR` is set (the worker mounts
`/data/protenix/module_cache` → `/cache/module_cache` and sets the env, see
`backend/worker/docker_cmd.py::protenix_runtime_mounts`), the fully-built
module is pickled once and loaded in seconds on later tasks:

- keyed by the config (per-task fields stripped) + checkpoint identity + torch
  version — a stale cache can never be reused;
- any load failure falls back to standard construction;
- verified on CDK2: cold 125.7 s → warm 37.9 s (score) / 44.3 s (dock);
  cold/warm outputs differ only by ordinary GPU forward nondeterminism
  (same magnitude as warm-vs-warm).

### 训练数据生成（结构型 shard）

`generate_structure_shards.py` 用 protenix2dock 的 dock 模式为 curated 记录批量生成
结构型训练数据（配体无共晶姿态时）：

```bash
python generate_structure_shards.py \
  --records records.json \          # [{"name","smiles","pic50"}, ...]
  --protein target_structure.pdb \
  --center_x .. --center_y .. --center_z .. --size_x 20 --size_y 20 --size_z 20 \
  --out /data/affinity_training/<target>_struct --gpus 0,0,1,1,2,2,3,3
```

- 每个记录：protenix2dock dock → 从**同一个输出复合物 cif** 提取蛋白链 + 配体链
  （二者天然同帧，无需任何事后对齐）；配体原子序与输入 SMILES 对齐校验一致才写入
- 输出：`<out>/proteins/<name>.pdb` + `<out>/poses/<name>.sdf` + `<out>/index.csv`
  （train_affinity.py 的 `--index_csv` 格式：name,pic50,active,protein_path,ligand_path）
- 可续跑（已有姿态自动跳过）；失败记录写入 `failures.csv`
- 吞吐（RTX 4090，模块缓存暖，2 worker/GPU）：~26s 墙钟/配体/GPU
- CDK2（P24941, 1H1Q 结构, 9058 复合物）：约 17 h（4 GPU × 2 worker）

### Training

```bash
# inside the Protenix runtime image:
python train_affinity.py --index_csv data.csv \
  # data.csv: name,pic50,active,protein_path,ligand_path  (PDBbind-style)
  --msa_prob 0.5 --epochs 20 --work_dir /out
# smoke (synthetic record, verifies the full train→save→reload→infer loop):
python train_affinity.py --smoke
```

Training runs the **frozen trunk** on each record through the *same*
`InferenceDataset.process_one` featurization as inference (MSA on/off
randomised), then trains the head on the crystal pose coordinates. Loss
follows the published Boltz-2/nesso-1 recipe: **Huber** on the value +
**focal** BCE on the binder score + an up-weighted **intra-assay
relative-difference** term (`--rel_weight`, Nesso-1's ranking optimisation;
assay-grouped partner sampling at 50%). Loss and readout see exactly the
distribution the fused inference path produces.

Data: curate raw assay exports with `prepare_affinity_data.py` (measurement
type/temperature filters, assay-variance floor, PAINS + heavy-atom + MW
window, MW-correlation assay guard, median dedup, **90%-identity
cluster-disjoint splits**, and a per-split MW-baseline Spearman audit).
See `docs/affinity_training_data.md` for the full curation study
(Boltz-2 Table 1 five-source recipe, Nesso-1 leakage guards, and the
recommended PDBbind→BindingDB two-stage training path).

### Evaluation roadmap

Train on PDBbind general + refinement sets (or BindingDB), then evaluate:
1. cdk8/10-target FEP series (same protocol as the bridge benchmark) —
   target: beat the +0.40 cross-engine bridge with the native head;
2. MSA-off ablation (nesso-style robustness check);
3. Compare `affinity_pred_value` vs `affinity_pred_std`-weighted ranking.

Status (first real-data cycle, cdk8-33 evaluation on **p2d's own dock poses**):

| signal | Spearman vs pIC50 |
| --- | --- |
| native head alone (60-sample × 3-epoch pilot — underfit by design) | +0.10 |
| **fusion(native value, ipsae_dom)** | **+0.476** (p=0.005) |
| cross-engine bridge baseline (boltz2 affinity on same poses) | +0.404 |
| nesso-1 (no pose, fully trained) | +0.754 |

The full pipeline is verified end-to-end on real data: BindingDB 202608
download (9 GB TSV) → 2M-row conversion → 300k curation (216k kept,
MW-baseline audit 0.31-0.42) → structure-free training (trunk distogram
expected-distance channel, Nesso-1 style) → checkpoint → fused inference on
p2d poses (pose coordinates flow: dock CIF → score-only channel → head
distance channel). Even the underfit pilot fused with ipsae_dom already
beats the cross-engine bridge; scaling to the full curated set
(`train_affinity.py --index_csv curated_300k/train.csv`, ~4 GPU-days/epoch
single-card) is the remaining step to a competitive native head.

## How it works

1. **Input prep** (`core/structure.py`, `core/input_prep.py`): parses the
   protein structure with gemmi, drops crystallographic artifacts (waters,
   ions, buffers, caps), normalizes non-standard residues (CCD modifications
   where possible), embeds/poses the ligand (dock: ETKDG conformer translated
   to the pocket centre; other modes: the provided SDF pose), resolves MSAs
   (V-Bio's md5-keyed cache first, then the ColabFold server), and builds the
   Protenix input json.
2. **Atom-order alignment**: runs Protenix's own
   `SampleDictToFeatures` pipeline on that json, so user coordinates are
   aligned to exactly the atom order the model will assemble. Unmatched atoms
   (mask 0) fall back to the standard Gaussian start.
3. **Engine patches** (vendored fork, env-driven — same style as
   `PROTENIX_LOW_VRAM`):
   - `PROTENIX_INIT_COORDS_PATH`: diffusion initialises from the aligned
     input coordinates instead of pure noise
     (`protenix/model/generator.py::sample_diffusion(init_coords=…)`).
   - `PROTENIX_SCORE_ONLY=1`: bypasses diffusion; the confidence heads score
     the input coordinates directly (score mode).
   - `PROTENIX_TFG_CONTACTS_PATH`: injects ligand–pocket contact pairs into
     the TFG `PairwiseDistancePotential` feats (angle category so the upper
     bound survives — clash-category pairs have `upper=inf`), reproducing
     Boltz2Score's anchored-refinement constraints.
4. **Inference** (`core/runner.py`): the stock `InferenceRunner` with mode
   config overrides (`sample_diffusion.N_step/N_sample`,
   `inference_noise_scheduler.s_max`, `sample_diffusion.guidance.enable`).
5. **Outputs**: per-sample mmCIF + summary confidences under
   `<output>/<name>/seed_<seed>/predictions/`, plus a
   `protenix2dock_summary.json` with all confidences and the best sample
   (max ranking_score).

## Running (inside the Protenix runtime image)

```bash
docker run --rm --entrypoint= --gpus device=0 \
  -v /data/V-Bio:/workspace/vbio:ro \
  -v /data/protenix/common_cache:/cache/common:ro \
  -v /data/protenix/model:/workspace/model:ro \
  -v $OUT:$OUT -v $WORK_INPUTS:$WORK_INPUTS -v /dev/shm:/dev/shm \
  --env PYTHONPATH=/workspace/vbio/vendor/protenix-source \
  --env PROTENIX_ROOT_DIR=/cache \
  vbio-protenix-v2-runtime:2.0.0 \
  /usr/local/micromamba/envs/protenix/bin/python \
  /workspace/vbio/capabilities/protenix2dock/protenix2dock.py \
  --mode dock \
  --protein_file /inputs/target.pdb \
  --ligand_smiles 'CC(C)Cc1ccc(C(=O)O)cc1' \
  --center_x -4.8 --center_y 11.9 --center_z -9.7 \
  --target_chain A \
  --output_dir $OUT \
  --msa_server_url http://<host>:8080 \
  --seed 42 --low_vram
```

Notes:
- `--low_vram` sets `PROTENIX_LOW_VRAM=1` (bf16 trunk, chunked kernels, one
  diffusion sample at a time) — recommended on 24 GB cards.
- Affinity: Protenix has no affinity head; score output is confidence-only
  (iptm/ptm/plddt/ranking_score + chain-level breakdown).
- MSA cache: reads `/data/boltz_msa_cache` (same md5 keys as the V-Bio
  backend) when mounted at `--msa_cache_dir`.

## Cross-engine affinity bridge (`run_with_affinity.sh`)

Protenix produces the pose; the **Boltz2 affinity head** (from
`capabilities/boltz2score`) then scores that exact pose in a second
container. On the cdk8 benchmark (33 ligands) this lifts Spearman vs pIC50
from **+0.28** (protenix confidence alone) to **+0.40** (p<0.05); the same
head on native Boltz2 poses reaches +0.70, so poses remain the bottleneck —
treat bridge numbers as a ranking signal, not calibrated pIC50.

Confidence-family metrics were also measured on both poses: iptm and
`ligand_ipsae_max` show no positive correlation with activity on this
series (interface_rank_score even trends negative), matching the upstream
benchmark note that only affinity outputs carry activity signal. IPSAE's
role stays what it was designed for: picking the best sample among
diffusion replicas, not ranking ligands.

### IPSAE on protenix poses (`--compute ipsae`-free, always on)

`protenix2dock` computes **IPSAE for every sample** post-hoc (pure numpy,
reuses `boltz2score/metrics/ligand_ipsae.py`, whose CIF token builder
handles Protenix tokenisation natively) using the token PAE matrix saved in
`*_full_data_sample_*.json`. Output: `*_ipsae_sample_*.json` per sample,
`ligand_ipsae_max`/`ipsae_dom`/`interface_pair_count` merged into the
summary, plus `best_by_interface` (0.5·ipsae_dom + 0.3·iptm +
0.2·ligand_ipsae_max).

cdk8 measurements (33 ligands, Spearman vs pIC50):

| signal | value |
| --- | --- |
| `ipsae_dom` (best sample / sample mean) | **+0.51 / +0.50** (p≈0.003) |
| protenix `ranking_score` (baseline) | +0.28 |
| `ligand_ipsae_max` | ≈ 0 |
| rank-fusion `ipsae_dom` + bridge affinity | **+0.55** (p<0.001) |

Notably IPSAE is *more* informative on protenix poses (+0.51) than on
Boltz2's own poses (+0.17): `ligand_ipsae_max` saturates on this series,
while `ipsae_dom` (global interface PAE confidence) is the dominant
confidence-family activity signal for this engine.

```bash
capabilities/protenix2dock/run_with_affinity.sh -- \
  --mode dock --protein_file /inputs/target.pdb \
  --ligand_smiles 'CC(C)Cc1ccc(C(=O)O)cc1' \
  --center_x -4.8 --center_y 11.9 --center_z -9.7 \
  --target_chain A --output_dir $OUT \
  --msa_server_url http://host:8080 --seed 42 --low_vram
```

Writes `affinity_bridge_summary.json` (protenix best pose + Boltz2
affinity/confidence on it) next to the engine's own summary. Requires both
runtime images and the shared mounts the script lists.
