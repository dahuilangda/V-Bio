# protenix2dock

Protein–ligand structure workflow on the Protenix engine, with mode
semantics matching Boltz2Score so the two engines are directly comparable
in benchmarks.

| Mode | Function | Schedule (sigma_max / steps / samples) |
| --- | --- | --- |
| `score` | Confidence scoring of an input pose. Diffusion is bypassed; the confidence heads evaluate the input coordinates. | — / — / 1 |
| `pose` | Refinement constrained to stay near the input pose | 0.02 / 8 / 5 |
| `refine` | Flexible refinement | 0.03 / 10 / 5 |
| `interface` | Interface-focused refinement | 0.04 / 12 / 5 |
| `dock` | SMILES to pocket placement ensemble against a rigid receptor: one rotation per diffusion sample, steric-floor anchors, contact-guided sampling (the small-sigma ladder keeps the TFG guidance projection well-conditioned) | 0.05 / 12 / 5 |
| `peptide` | Receptor-fixed peptide inpainting: the peptide enters as a proteinChain, the receptor is fixed every step, and bicyclic linkers carry explicit covalent-bond constraints. Used for mirror-space D-peptide design. | 0.05 / 12 / 8 |

Protenix's `InferenceNoiseScheduler` uses the same EDM parameterisation as
Boltz2 (`sigma_data = 16`), so `s_max == sigma_max` reproduces the
Boltz2Score schedule range (first-step noise = `16 × s_max` Å).

## Geometry constraints during diffusion

Anchor-guided diffusion corrects individual atoms, which by itself can
displace one atom off its residue while its bonded neighbours stay put.
Three constraint families therefore ride the same damped-Jacobi distance
projection after every diffusion step, and bonds project last (the ordering
of the official `PairwiseDistancePotential`: angles first, bonds last):

1. Pocket anchor bands `[d−0.3, d+0.3]` hold the free chain near its
   placed geometry.
2. VDW clash floors `0.35 + 0.5·(ri+rj)` (no upper bound, official
   `protenix.tfg.potentials` semantics) cover every receptor/free heavy-atom
   pair within 8 Å, so an atom moved into the receptor surface is pushed
   back out.
3. Covalent bond bands `[d−0.12, d+0.12]` cover every bond of the free
   chains, detected on the assembled atom table (the coordinates the
   sampler actually updates, including atoms rebuilt from CCD reference
   geometry) by all-pairs search with element-aware cut-offs (1.95 Å, 2.15 Å
   for sulfur pairs) and an element-pair plausibility filter. Corrections
   distribute through the bond network as torsion changes instead of
   detaching atoms.

The same bond set also enters the TFG soft channel with `is_bond=1`, so the
denoiser's x0 prediction is constrained by the same chemistry. In dock mode
the ligand's bonds come from the RDKit molecular graph instead of distance
detection.

## Interface metrics (`--interface_chains`)

Interface metrics are scoped to the chains the caller declares rather than
averaged over all pairs: `--interface_chains 'A,B'` (receptor vs
peptide/ligand) or `'AB,C'` (multi-chain receptor vs ligand). ipSAE runs
against the second group's ligand chain, `pair_iptm` is the weakest
cross-group pair from the engine's chain-pair matrix, and `ligand_plddt` is
the ligand chain's own plddt. The global iptm averages over every chain
pair, so a bicyclic linker pair (0.09) or a homodimer interface can dominate
it and misreport the requested interface.

`--blind` (dock mode) runs the engine's standard full-noise diffusion: no
pocket box, no placement ensemble, no receptor fixing, no TFG/anchors —
sequence and SMILES in, complex out.

## Native affinity head

`ProtenixAffinityHead` (`vendor/.../protenix/model/modules/affinity.py`) is
a 3.3M-parameter head integrated into the Protenix forward pass, not a
post-hoc stage:

- Called at the same site as the confidence head, inside the main inference
  loop; consumes the trunk's `s_inputs`/`z` representations plus the
  predicted or input coordinates. Per-sample outputs merge into
  `pred_dict["summary_confidence"]` and the stock dumper writes them
  (`affinity_pred_value`, `..._value1/2`, `..._probability_binary`,
  `..._pred_score`, `..._pred_std`).
- Interface evidence follows Boltz-2: minimal atom distance per interface
  token pair, binned and embedded, injected into `z` before the
  interface-focused pair attention (lig-rec, rec-lig, lig-lig pairs only).
- Uncertainty follows Nesso-1: MC-dropout produces `affinity_pred_std`, and
  a two-path value ensemble. Training randomises `use_msa` per sample
  (`--msa_prob 0.5`) so inference works without MSA.
- Computes in fp32 with autocast disabled, required under the bf16 trunk of
  `PROTENIX_LOW_VRAM`.
- Loaded through the `PROTENIX_AFFINITY_CKPT` side channel (or CLI
  `--affinity_head_ckpt`); construction is deferred until after the stock
  checkpoint loads, leaving the main weights untouched.

### Module cache

Model construction costs about 80 s per task — every layer is built with
random weights that `load_checkpoint` immediately overwrites. When
`PROTENIX_MODULE_CACHE_DIR` is set (the worker mounts
`/data/protenix/module_cache` at `/cache/module_cache`; see
`backend/worker/docker_cmd.py::protenix_runtime_mounts`), the built module
is pickled once and reloaded in seconds:

- cache keys cover the config (per-task fields stripped), checkpoint
  identity and torch version, so a stale entry is never reused;
- any load failure falls back to standard construction;
- measured on CDK2: 125.7 s cold to 37.9 s warm (score) / 44.3 s (dock);
  cold/warm differences are within ordinary GPU forward nondeterminism.

### Structure-based training shards

`generate_structure_shards.py` produces structure-mode training data with
dock mode for curated records whose ligands have no crystal pose:

```bash
python generate_structure_shards.py \
  --records records.json \          # [{"name","smiles","pic50"}, ...]
  --protein target_structure.pdb \
  --center_x .. --center_y .. --center_z .. --size_x 20 --size_y 20 --size_z 20 \
  --out /data/affinity_training/<target>_struct --gpus 0,0,1,1,2,2,3,3
```

- Each record docks once; the protein and ligand chains are extracted from
  the same output complex CIF (same frame, no post-alignment), and the
  ligand atom order is verified against the input SMILES before writing.
- Output: `<out>/proteins/<name>.pdb`, `<out>/poses/<name>.sdf`,
  `<out>/index.csv` (the `--index_csv` format of `train_affinity.py`:
  name,pic50,active,protein_path,ligand_path). Resumable; failures are
  recorded in `failures.csv`.
- Throughput on RTX 4090 with a warm module cache, two workers per GPU:
  about 26 s per ligand per GPU. CDK2 (P24941, 1H1Q structure, 9058
  complexes) takes roughly 17 h on 4 GPUs.

### Training

```bash
# inside the Protenix runtime image:
python train_affinity.py --index_csv data.csv \
  # data.csv: name,pic50,active,protein_path,ligand_path  (PDBbind-style)
  --msa_prob 0.5 --epochs 20 --work_dir /out
# smoke test (synthetic record; verifies train -> save -> reload -> infer):
python train_affinity.py --smoke
```

Training runs the frozen trunk on each record through the same
`InferenceDataset.process_one` featurization as inference (MSA on/off
randomised) and fits the head on the crystal poses. The loss follows the
published Boltz-2/Nesso-1 recipe: Huber on the value, focal BCE on the
binder score, and an up-weighted intra-assay relative-difference term
(`--rel_weight`; assay-grouped partner sampling at 50%).

Curate raw assay exports with `prepare_affinity_data.py` (measurement
type/temperature filters, assay-variance floor, PAINS and size windows,
MW-correlation assay guard, median deduplication, 90%-identity
cluster-disjoint splits, and a per-split MW-baseline Spearman audit).
`docs/affinity_training_data.md` documents the curation study.

### Evaluation status

First real-data cycle (cdk8-33, scored on this tool's own dock poses):

| Signal | Spearman vs pIC50 |
| --- | --- |
| native head alone (60-sample pilot, 3 epochs, underfit by design) | +0.10 |
| fusion of native value with ipsae_dom | +0.476 (p=0.005) |
| cross-engine bridge (Boltz2 affinity on the same poses) | +0.404 |
| Nesso-1 reference (no pose, fully trained) | +0.754 |

The pipeline has been verified end to end on real data: BindingDB 202608
(9 GB TSV) to a 2M-row conversion to a 300k curation (216k kept; MW-baseline
audit 0.31–0.42), structure-free training (trunk distogram
expected-distance channel, Nesso-1 style), and fused inference on docked
poses. Remaining step: scale training to the full curated set
(`train_affinity.py --index_csv curated_300k/train.csv`, about 4 GPU-days
per epoch on a single card).

## Pipeline

1. Input preparation (`core/structure.py`, `core/input_prep.py`): parses the
   protein with gemmi, removes crystallographic artifacts (waters, ions,
   buffers, caps), normalises non-standard residues (CCD modifications where
   available), embeds or places the ligand (dock: ETKDG conformer
   translated to the pocket centre; other modes: the provided SDF pose),
   resolves MSAs (the md5-keyed shared cache first, then the ColabFold
   server), and builds the Protenix input json.
2. Atom-order alignment: the same `SampleDictToFeatures` pipeline runs on
   that json, so user coordinates map onto exactly the atom order the model
   assembles. Atoms without a source coordinate (mask 0) fall back to the
   Gaussian start unless rebuilt from CCD reference geometry.
3. Engine integration (vendored tree, environment-driven):
   - `PROTENIX_INIT_COORDS_PATH`: diffusion starts from the aligned input
     coordinates instead of pure noise.
   - `PROTENIX_SCORE_ONLY=1`: bypasses diffusion; the confidence heads
     score the input coordinates (score mode).
   - `PROTENIX_PIN_MASK_PATH`: per-step receptor fixing (true inpainting).
   - `PROTENIX_ANCHOR_PAIRS_PATH` / `PROTENIX_COVALENT_BONDS_PATH`: the
     pocket, clash-floor and covalent-bond distance bands of the geometry
     constraint section above.
   - `PROTENIX_TFG_CONTACTS_PATH`: ligand–pocket contact pairs injected
     into the TFG `PairwiseDistancePotential` feats (angle category so the
     upper bound survives; clash-category pairs have `upper=inf`),
     reproducing Boltz2Score's anchored refinement.
4. Inference (`core/runner.py`): the stock `InferenceRunner` with mode
   config overrides (`sample_diffusion.N_step/N_sample`,
   `inference_noise_scheduler.s_max`, `sample_diffusion.guidance.enable`).
5. Outputs: per-sample mmCIF and summary confidences under
   `<output>/<name>/seed_<seed>/predictions/`, plus
   `protenix2dock_summary.json` with all confidences and the best sample by
   `ranking_score`.

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

- `--low_vram` sets `PROTENIX_LOW_VRAM=1` (bf16 trunk, chunked kernels, one
  diffusion sample at a time); recommended on 24 GB cards.
- Score output is confidence-only (iptm/ptm/plddt/ranking_score plus the
  chain-level breakdown); affinity values require the trained head.
- The MSA cache reads `/data/boltz_msa_cache` (the same md5 keys as the
  V-Bio backend) when mounted at `--msa_cache_dir`.

## Cross-engine affinity bridge (`run_with_affinity.sh`)

Protenix produces the pose; the Boltz2 affinity head
(`capabilities/boltz2score`) scores that pose in a second container. On the
cdk8 benchmark (33 ligands) this raises Spearman vs pIC50 from +0.28
(Protenix confidence alone) to +0.40 (p<0.05); the same head on native
Boltz2 poses reaches +0.70, so the pose remains the bottleneck — treat
bridge numbers as a ranking signal, not calibrated pIC50.

On this series, iptm and `ligand_ipsae_max` show no positive correlation
with activity, consistent with the upstream benchmark note that only
affinity outputs carry activity signal. ipSAE keeps its designed role:
selecting the best sample among diffusion replicas.

### ipSAE on Protenix poses

ipSAE is computed post-hoc for every sample (numpy; reuses
`boltz2score/metrics/ligand_ipsae.py`, whose CIF token builder handles
Protenix tokenisation) from the token PAE matrix in
`*_full_data_sample_*.json`. Output: `*_ipsae_sample_*.json` per sample;
`ligand_ipsae_max`, `ipsae_dom` and `interface_pair_count` merge into the
summary, together with `best_by_interface` (0.5·ipsae_dom + 0.3·iptm +
0.2·ligand_ipsae_max).

cdk8 measurements (33 ligands, Spearman vs pIC50):

| Signal | Value |
| --- | --- |
| `ipsae_dom` (best sample / sample mean) | +0.51 / +0.50 (p≈0.003) |
| Protenix `ranking_score` (baseline) | +0.28 |
| `ligand_ipsae_max` | ≈ 0 |
| rank fusion of `ipsae_dom` and bridge affinity | +0.55 (p<0.001) |

ipSAE carries more activity signal on Protenix poses (+0.51) than on
Boltz2's own poses (+0.17): `ligand_ipsae_max` saturates on this series
while `ipsae_dom` (global interface PAE confidence) dominates.

```bash
capabilities/protenix2dock/run_with_affinity.sh -- \
  --mode dock --protein_file /inputs/target.pdb \
  --ligand_smiles 'CC(C)Cc1ccc(C(=O)O)cc1' \
  --center_x -4.8 --center_y 11.9 --center_z -9.7 \
  --target_chain A --output_dir $OUT \
  --msa_server_url http://host:8080 --seed 42 --low_vram
```

Writes `affinity_bridge_summary.json` (Protenix best pose plus Boltz2
affinity/confidence on it) next to the engine's own summary. Requires both
runtime images and the shared mounts listed in the script.
