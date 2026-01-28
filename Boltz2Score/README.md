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

## Usage

### One-step (single PDB/mmCIF)

```bash
python Boltz2Score/boltz2score.py \
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
python Boltz2Score/prepare_boltz2score_inputs.py \
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

### 2) Run score‑only inference

```bash
python Boltz2Score/run_boltz2score.py \
  --processed_dir /path/to/boltz2score_job/processed \
  --output_dir /path/to/boltz2score_job/predictions
```

Optional flags:

- `--checkpoint /path/to/boltz2_conf.ckpt`
- `--cache /path/to/boltz_cache`
- `--output_format mmcif|pdb`
- `--recycling_steps 3` (default)

### 3) Collect metrics into CSV

```bash
python Boltz2Score/collect_boltz2score_metrics.py \
  --pred_dir /path/to/boltz2score_job/predictions \
  --output_csv /path/to/boltz2score_job/boltz2score_metrics.csv
```

## Notes

- The output structure is written from the **featurized coordinates**, which are centered by the featurizer. The geometry is preserved, but absolute position is not. If you need the original coordinates, keep the input PDB/mmCIF alongside the confidence JSON.
- MSA is disabled (single‑sequence dummy MSA). If you want to use MSAs, you can extend the manifest with valid MSA ids and place `.npz` files in `processed/msa`.

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
