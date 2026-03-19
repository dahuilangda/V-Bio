# Boltz2Score

Boltz2Score scores an existing protein-ligand complex with the Boltz-2 confidence head and optionally performs pose-conditioned local optimization.

The main entrypoint is `boltz2score.py` with four modes:

- `score`: confidence scoring only
- `pose`: minimal-drift local optimization around the input pose
- `refine`: stronger local optimization with a balance between pose retention and interface quality
- `interface`: the loosest optimization mode, biased toward interface improvement

## Install

```bash
cd /data/Boltz2Score
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Optional CUDA build:

```bash
pip install --upgrade "boltz[cuda]"
```

Required Boltz cache assets under `BOLTZ_CACHE` or `~/.boltz`:

- `ccd.pkl`
- `mols/`
- `boltz2_conf.ckpt`

Optional affinity assets:

- `boltz2_aff.ckpt`

## Quick Start

### Score only

```bash
python boltz2score.py \
  --protein_file data/cdk8/5hnb-chainA-prepared.pdb \
  --ligand_file data/cdk8/ligands.sdf \
  --output_dir results/cdk8_score \
  --compute_ipsae
```

### Pose optimization

```bash
python boltz2score.py \
  --mode pose \
  --protein_file data/cdk8/5hnb-chainA-prepared.pdb \
  --ligand_file data/cdk8/ligands.sdf \
  --output_dir results/cdk8_pose \
  --compute_ipsae
```

### Refine optimization

```bash
python boltz2score.py \
  --mode refine \
  --protein_file data/cdk8/5hnb-chainA-prepared.pdb \
  --ligand_file data/cdk8/ligands.sdf \
  --output_dir results/cdk8_refine \
  --compute_ipsae
```

### Interface optimization

```bash
python boltz2score.py \
  --mode interface \
  --protein_file data/cdk8/5hnb-chainA-prepared.pdb \
  --ligand_file data/cdk8/ligands.sdf \
  --output_dir results/cdk8_interface \
  --compute_ipsae
```

### Affinity prediction

```bash
python boltz2score.py \
  --protein_file data/cdk8/5hnb-chainA-prepared.pdb \
  --ligand_file data/cdk8/ligands.sdf \
  --output_dir results/cdk8_affinity \
  --enable_affinity \
  --target_chain A \
  --ligand_chain L
```

Use `--affinity_refine` if you want the official Boltz2 affinity head to run on a refined pre-affinity structure instead of the default fast path.
Affinity runs only when you explicitly pass `--enable_affinity` together with `--target_chain` and `--ligand_chain`, and is currently supported only for protein-small-molecule complexes. Other input types still run normal Boltz2Score scoring/refinement, but skip affinity.

## Inputs

- Use either `--input <complex.pdb/mmcif>` or `--protein_file + --ligand_file`.
- Optimization modes currently require separate-input mode with an SDF ligand file.
- Multi-molecule SDF is supported. Each valid ligand becomes one record directory.
- Use `--ligand_indices 1,3-5` to run only selected 1-based ligand entries from a multi-molecule SDF.
- If ligand SMILES are not provided, RDKit derives canonical SMILES from the ligand structure.
- With `--compute_ipsae`, output is forced to mmCIF.

Optional ligand SMILES override:

```bash
python boltz2score.py \
  --input /path/to/complex.pdb \
  --output_dir /path/to/out \
  --ligand_smiles_map '{"L:LIG":"CC1=CC=CC=C1"}'
```

Accepted `ligand_smiles_map` keys:

- `chain`
- `chain:resname`

## Output Layout

Each record directory contains:

- `best_model.cif`
- `best_confidence.json`
- `affinity_<record>.json` when affinity is enabled
- `best_ipsae.json` when `--compute_ipsae` is enabled
- per-sample `confidence_<record>_model_*.json`
- per-sample structure files
- per-sample `ipsae_<record>_model_*.json` when IPSAE is enabled

Ligand atom confidence is exposed in three explicit orders inside `confidence_*.json`:

- input heavy-atom order: `ligand_atom_plddts`, `ligand_atom_names`
- RDKit traversal order: `ligand_atom_smiles_order_plddts`, `ligand_atom_smiles_order_names`
- Boltz writer/model order: `ligand_atom_model_order_plddts`, `ligand_atom_model_order_names`

## Mode Summary

### `score`

- No diffusion resampling
- Best baseline for pure confidence scoring

### `pose`

- Most conservative optimization mode
- Best when the input ligand orientation is already plausible
- Default choice for docking-pose cleanup

### `refine`

- Middle ground between structure retention and interface adjustment
- Useful when pose quality is uncertain

### `interface`

- Most permissive optimization mode
- Favors interface-focused confidence more than strict pose retention

## IPSAE

Reported IPSAE-related fields include:

- `ipsae_dom`: raw interface-wide IPSAE
- `ligand_ipsae_max`: strongest local ligand-contact IPSAE

## Advanced Flags

Most users should stay with `--mode` and `--compute_ipsae`.

Lower-level refinement flags are still available for method work:

- `--structure_refine`
- `--anchored_refine`
- `--reference_from_input`
- `--sampling_init_from_input`
- `--sigma_max`
- `--noise_scale`
- `--gamma_0`
- `--gamma_min`

## Repository Layout

- `core/`: orchestration, CLI, input prep, inference, and result handling
- `utils/`: ligand handling, diagnostics, writer compatibility, and refinement helpers
- `metrics/`: metric calculation modules
- `tools/`: helper scripts
- `data/`: benchmark and plotting scripts

## Notes

- Boltz2 confidence scores are not experimental affinity predictions.
- Affinity prediction uses the official Boltz2 affinity checkpoint and requires `boltz2_aff.ckpt` in the Boltz cache.
- Flexible optimization is an engineering layer on top of Boltz2, not the original Boltz2 inference workflow.
- If Numba cache permissions are problematic, set `NUMBA_CACHE_DIR=/tmp/numba_cache`.
