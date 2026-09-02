# Boltz2Score

Boltz2Score scores an existing protein-ligand complex with the Boltz-2 confidence head and optionally performs pose-conditioned local optimization.

The main entrypoint is `boltz2score.py` with five modes:

- `score`: confidence scoring only
- `pose`: minimal-drift local optimization around the input pose
- `refine`: stronger local optimization with a balance between pose retention and interface quality
- `interface`: the loosest optimization mode, biased toward interface improvement
- `dock`: **flexible docking from SMILES** — generates a 3-D conformer, places it at the binding pocket, and refines with anchored diffusion. No external docking software needed.

## Install

```bash
cd capabilities/boltz2score
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

The example inputs under `data/` are ordinary prepared structures (PDB down-
loaded from the RCSB, ligands as SDF); substitute any protein–ligand pair of
your own.

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

### Flexible docking (dock mode)

Dock mode takes **SMILES** instead of a pre-placed 3-D ligand. It generates an ETKDG conformer, places it at the binding pocket (the placement defines the pocket for the contact constraints), and runs the engine's **generation** diffusion schedule (sigma_max 160, 200 steps, 16 samples) so the structure module re-poses the ligand conditioned on the fixed input protein — no external docking software required. The calibrated 0.02–0.05 sigma ladder is reserved for the refinement modes (pose/refine/interface); hand-tuned intermediate sigmas collapse flexible ligands.

Three ways to define the pocket (pick one):

```bash
# Center coordinates + box size
python boltz2score.py \
  --mode dock \
  --protein_file rec.pdb \
  --ligand_smiles "c1ccccc1" \
  --center_x 10.5 --center_y 20.3 --center_z -5.0 \
  --size_x 20 --size_y 20 --size_z 20 \
  --output_dir results/dock

# Reference ligand
python boltz2score.py \
  --mode dock \
  --protein_file rec.pdb \
  --ligand_smiles "c1ccccc1" \
  --pocket_ligand crystal_ligand.pdb \
  --output_dir results/dock

# Pocket residues (Cα centroid)
python boltz2score.py \
  --mode dock \
  --protein_file rec.pdb \
  --ligand_smiles "c1ccccc1" \
  --pocket_residues "A:100,A:101,A:102,A:103" \
  --output_dir results/dock
```

Shorthand `--pocket_center "x,y,z"` is also accepted as an alternative to `--center_x/y/z`.
When `--size_x/y/z` are set, the pocket search radius is derived as half the largest box dimension; otherwise `--pocket_radius` (default 7.0 Å) is used.

Batch processing from a SMILES file (`.smi` = `SMILES\tname` per line, or `.csv` with a `SMILES` column):

```bash
python boltz2score.py \
  --mode dock \
  --protein_file rec.pdb \
  --ligand_smiles_file compounds.smi \
  --pocket_ligand crystal_ligand.pdb \
  --enable_affinity --target_chain A --ligand_chain L \
  --output_dir results/dock_batch
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

### Confidence scoring without affinity

To run the confidence head only (skip the affinity head entirely), omit `--enable_affinity`:

```bash
python boltz2score.py \
  --protein_file data/cdk8/5hnb-chainA-prepared.pdb \
  --ligand_file data/cdk8/ligands.sdf \
  --output_dir results/cdk8_confidence \
  --compute_ipsae
```

### Affinity prediction without confidence scoring

To run the affinity head only (skip the confidence head), use `--enable_affinity` without `--compute_ipsae` and without structure refinement:

```bash
python boltz2score.py \
  --protein_file data/cdk8/5hnb-chainA-prepared.pdb \
  --ligand_file data/cdk8/ligands.sdf \
  --output_dir results/cdk8_affinity_only \
  --enable_affinity \
  --target_chain A \
  --ligand_chain L
```

## Inputs

- Use either `--input <complex.pdb/mmcif>` or `--protein_file + --ligand_file`.
- Optimization modes (pose/refine/interface) require separate-input mode with an SDF ligand file.
- Dock mode uses `--ligand_smiles` (or `--ligand_smiles_file`) instead of `--ligand_file`, plus a pocket definition (`--pocket_ligand` / `--pocket_center` / `--pocket_residues`).
- Multi-molecule SDF is supported. Each valid ligand becomes one record directory.
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

When affinity is enabled, `affinity_<record>.json` contains:

- `affinity_pred_value`: raw Boltz-2 affinity head output (≈ log10 IC50 in µM)
- `affinity_pic50`: converted to pIC50 scale (`6 − affinity_pred_value`)
- `affinity_pred_value_mw`: MW-calibrated affinity (1.035·v − 0.600·MW^0.3 + 2.833)
- `affinity_pic50_mw`: MW-calibrated pIC50
- `affinity_probability_binary`: binary binding probability
- `affinity_pic501` / `affinity_pic502`: per-head ensemble pIC50 values

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

### `dock`

- Flexible docking from SMILES — no pre-placed ligand required
- Generates an ETKDG conformer and places it at the binding pocket; the placement defines the pocket for the contact constraints
- Runs the engine's **generation** schedule (sigma_max 160, 200 sampling steps, 16 samples) so the structure module re-poses the ligand conditioned on the input protein + pocket contact guidance — the 0.02–0.05 sigma ladder is for the refinement modes, not for docking (hand-tuned intermediate sigmas collapse flexible ligands)
- GPU-verified on the CDK2 co-crystal ligand (1H1Q): blind dock scores ligand pLDDT 83.0 / iptm 0.972 / ipSAE 0.731 vs 79.1 / 0.969 for the native co-crystal pose, with the protein held at the input structure (CA RMSD 0.38 Å)
- `--dock_poses N` runs a pose ensemble (N diverse initial conformer/orientation placements per SMILES); the best-scored pose per ligand is kept (`dock_ensemble_selection.json`)
- Pocket can be defined via reference ligand, explicit coordinates, or key residues
- Suitable for virtual screening: confidence and affinity predictions are comparable to docked-pose inputs

## IPSAE

Reported IPSAE-related fields include:

- `ipsae_dom`: raw interface-wide IPSAE
- `ligand_ipsae_max`: strongest local ligand-contact IPSAE

## Advanced Flags

Most users should stay with `--mode` and `--compute_ipsae`.

Recycling and precision defaults (score mode):

- Confidence head recycling defaults to **R = 1** (Boltz-2 upstream default is 20). At R = 1, confidence scores change by less than 0.5%. Pass `--recycling_steps 20` for bit-for-bit upstream parity.
- Affinity head recycling defaults to **R = 1** (upstream default is 5). Override with `--affinity_recycling_steps`.
- Precision defaults to **bf16-mixed** for all modes. The model internally force-casts precision-critical blocks (structure module, affinity head) to fp32, so bf16-mixed is safe even for refinement modes. Pass `--trainer_precision 32` for pure fp32.

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
- `utils/`: ligand handling, diagnostics, writer compatibility, refinement helpers, and flexible-docking utilities
- `metrics/`: metric calculation modules
- `tools/`: helper scripts
- `data/`: benchmark and plotting scripts

## Notes

- Boltz2 confidence scores are not experimental affinity predictions.
- Affinity prediction uses the official Boltz2 affinity checkpoint and requires `boltz2_aff.ckpt` in the Boltz cache.
- Score mode loads both checkpoints once per batch and reuses them across all ligands. Optimization modes (pose/refine/interface/dock) load per ligand via the pipeline.
- Flexible optimization is an engineering layer on top of Boltz2, not the original Boltz2 inference workflow.
- If Numba cache permissions are problematic, set `NUMBA_CACHE_DIR=/tmp/numba_cache`.
