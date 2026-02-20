# Boltz2Score

Score existing structures with the Boltz-2 confidence head **without running diffusion structure prediction**. This mirrors the AF3Score idea of “prediction stripped, scoring only” by setting `skip_run_structure=True` and feeding your input coordinates directly into the confidence module.

## What it does

- Parses input PDB/mmCIF structures into Boltz2 `StructureV2` records.
- Runs the Boltz2 trunk + confidence head **only** (no diffusion sampling).
- Writes confidence summaries (`confidence_*.json`) and a copy of the structure with pLDDT in B‑factors (optional output format).

## Requirements

You need the Boltz2 cache assets:

- `ccd.pkl`
- `mols/` directory
- `boltz2_conf.ckpt`

By default the scripts use `BOLTZ_CACHE` or `~/.boltz` (same as Boltz CLI).

## Installation

### Standalone install

```bash
cd /path/to/Boltz2Score

# 1) Create isolated env
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip

# 2) Install runtime deps
pip install "boltz[cuda]"
```

If your machine has no CUDA GPU, replace the last line with:

```bash
pip install boltz
```

Then prepare Boltz cache assets (required):

```bash
export BOLTZ_CACHE=/path/to/boltz_cache
mkdir -p "$BOLTZ_CACHE"
# Put these files under $BOLTZ_CACHE:
#   ccd.pkl
#   mols/
#   boltz2_conf.ckpt
```

After that, run scripts directly from the standalone directory, e.g.:

```bash
python boltz2score.py --input /path/to/structure.pdb --output_dir /path/to/out
```

### Optional: enable affinity scoring

Affinity requires an additional external module and checkpoint. No extra service setup is required for single-process lightweight usage.

1) Add `affinity/` module beside this project (or on `PYTHONPATH`):

- Source: https://github.com/dahuilangda/Boltz-WebUI/tree/main/affinity
- Expected layout example:

```text
<workspace>/
  Boltz2Score/
    boltz2score.py
    prepare_boltz2score_inputs.py
    run_boltz2score.py
  affinity/
    __init__.py
    main.py
    boltzina/
```

2) Put affinity checkpoint into cache:

```bash
export BOLTZ_CACHE=/path/to/boltz_cache
# Required for affinity:
#   $BOLTZ_CACHE/boltz2_aff.ckpt
```

3) Run one-step scoring with chain flags to trigger affinity:

```bash
python boltz2score.py \
  --input /path/to/complex.pdb \
  --output_dir /path/to/out \
  --target_chain A \
  --ligand_chain B
```

If `affinity/` or `boltz2_aff.ckpt` is missing, the script keeps confidence scoring and skips affinity with a warning.

## Usage

### One-step (single PDB/mmCIF)

```bash
python boltz2score.py \
  --input /path/to/structure.pdb \
  --output_dir /path/to/boltz2score_out
```

Optional flags:

- `--cache /path/to/boltz_cache`
- `--checkpoint /path/to/boltz2_conf.ckpt`
- `--output_format mmcif|pdb`
- `--num_workers 0` (default)
- `--keep_work` (keep intermediates)
- `--target_chain A --ligand_chain B` (enable affinity prediction)

### 1) Prepare processed inputs

```bash
python prepare_boltz2score_inputs.py \
  --input_dir /path/to/pdbs \
  --output_dir /path/to/boltz2score_job
```

This creates:

```
/path/to/boltz2score_job/processed/
  structures/*.npz
  records/*.json
  manifest.json
  msa/
```

### 1.1) Prepare processed inputs with MSA (ColabFold server)

If you want Boltz2Score to use MSA, enable MSA generation during the prepare step:

```bash
python prepare_boltz2score_inputs.py \
  --input_dir /path/to/pdbs \
  --output_dir /path/to/boltz2score_job \
  --use_msa_server \
  --msa_server_url https://api.colabfold.com \
  --msa_pairing_strategy greedy \
  --max_msa_seqs 8192 \
  --msa_cache_dir /tmp/boltz_msa_cache
```

You can also point to a local ColabFold server, for example:

```bash
python prepare_boltz2score_inputs.py \
  --input_dir /path/to/pdbs \
  --output_dir /path/to/boltz2score_job \
  --use_msa_server \
  --msa_server_url http://localhost:8080
```

For local deployment, run any ColabFold-compatible API service and pass its URL via `--msa_server_url`.

Quick API health check for local server:

```bash
curl -X POST \
  -d $'q=>query\nMKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQANL' \
  -d 'mode=colabfold' \
  http://localhost:8080/ticket/msa
```

Equivalent env-style setup:

```bash
export MSA_SERVER_URL=http://localhost:8080
python prepare_boltz2score_inputs.py \
  --input_dir /path/to/pdbs \
  --output_dir /path/to/boltz2score_job \
  --use_msa_server
```

When `--use_msa_server` is on:

- Protein chains get MSA ids in `processed/records/*.json` (`msa_id` is no longer `-1`).
- Processed MSA files are written to `processed/msa/*.npz` and consumed directly by `run_boltz2score.py`.
- Raw server-returned CSV files are kept under `processed/msa/<target>_raw/`.
- Sequence-level `.a3m` cache is stored in `--msa_cache_dir` (default `/tmp/boltz_msa_cache`) as `msa_<md5>.a3m`.


### 2) Run score‑only inference

```bash
python run_boltz2score.py \
  --processed_dir /path/to/boltz2score_job/processed \
  --output_dir /path/to/boltz2score_job/predictions
```

Optional flags:

- `--checkpoint /path/to/boltz2_conf.ckpt`
- `--cache /path/to/boltz_cache`
- `--output_format mmcif|pdb`
- `--recycling_steps` (default: `7` in score-only mode, `3` with `--structure_refine`)

### 3) Collect metrics into CSV

```bash
python collect_boltz2score_metrics.py \
  --pred_dir /path/to/boltz2score_job/predictions \
  --output_csv /path/to/boltz2score_job/boltz2score_metrics.csv
```

## Notes

- The output structure is written from the **featurized coordinates**, which are centered by the featurizer. The geometry is preserved, but absolute position is not. If you need the original coordinates, keep the input PDB/mmCIF alongside the confidence JSON.
- If `prepare_boltz2score_inputs.py` is run without `--use_msa_server`, Boltz2Score falls back to single-sequence dummy MSA behavior.
- Affinity is optional and only available in `boltz2score.py` one-step mode with `--target_chain` + `--ligand_chain`. In standalone `Boltz2Score` copies, affinity may be unavailable unless the external `affinity` module and `boltz2_aff.ckpt` are also provided.

## Outputs

For each input ID, the output directory contains:

```
<ID>/
  <ID>_model_0.cif (or .pdb)
  confidence_<ID>_model_0.json
  chain_map.json
  affinity_<ID>.json (only if --target_chain and --ligand_chain are set)
```

The confidence JSON includes:

- `ptm`, `iptm`, `complex_plddt`, `complex_pde`, `confidence_score`, etc.
- `pair_chains_iptm` for chain‑pair scores

## Acknowledgements

Thanks to the following works for their inspiration and guidance:

- https://pubs.acs.org/doi/10.1021/acs.jcim.5c00653
- https://openreview.net/forum?id=OwtEQsd2hN
