# Boltz2Score

Boltz2Score provides confidence scoring for protein complexes and can optionally
run affinity prediction for protein-ligand systems.

## Usage

```bash
python boltz2score.py \
  --input /path/to/complex.pdb \
  --output_dir /path/to/output \
  --work_dir /path/to/work \
  --target_chain A \
  --ligand_chain B \
  --affinity_refine \
  --enable_affinity
```

### Arguments

- `--input`: Input PDB or CIF file.
- `--output_dir`: Output directory for result files.
- `--work_dir`: Working directory for intermediate files.
- `--target_chain`: Target protein chain IDs (comma-separated).
- `--ligand_chain`: Ligand chain IDs (comma-separated).
- `--affinity_refine`: Run diffusion refinement before affinity prediction.
- `--enable_affinity`: Force affinity prediction.
- `--auto_enable_affinity`: Enable affinity automatically when ligands are detected.

## Output

Each run writes a per-record folder under `output_dir` containing:

- `confidence_*.json`
- `chain_map.json`
- `plddt_*.npz`
- `pae_*.npz`
- `*_model_0.cif`
- `affinity_*.json` (when affinity prediction is enabled and succeeds)

## Acknowledgements

Thanks to the following works for their inspiration and guidance:

- https://pubs.acs.org/doi/10.1021/acs.jcim.5c00653
- https://openreview.net/forum?id=OwtEQsd2hN
