# Protenix2Score (Prototype)

`Protenix2Score` is a Docker-based scoring prototype aligned with the current Protenix runtime.

## Important Limitation

This is **not** equivalent to Boltz2Score's true "confidence-head-only, no-structure-generation" mode.

- `Boltz2Score` works because Boltz-2 exposes a practical score-only path on existing coordinates.
- Public Protenix runtime currently exposes inference/prediction flow and confidence outputs, but no direct score-only API on fixed coordinates in this repo.

So this prototype does:
1. Convert input PDB/mmCIF -> Protenix JSON (`protenix/data/inference/json_maker.py`)
2. Run Protenix inference in Docker (`runner/inference.py`)
3. Collect summary confidence JSONs into `protenix2score_metrics.json`

## Files

- `protenix2score.py`: single-run pipeline (convert + infer + collect metrics)
- `collect_protenix2score_metrics.py`: aggregate summary JSON files into CSV

## Usage

```bash
python Protenix2Score/protenix2score.py \
  --input /path/to/complex.pdb \
  --output_dir /path/to/protenix2score_out
```

Common options:

- `--model_name protenix_base_20250630_v1.0.0`
- `--use_msa false`
- `--use_template false`
- `--seed 101`
- `--prepare_only` (only test conversion, skip inference)
- `--docker_image ...`
- `--protenix_source /data/protenix`
- `--model_dir /data/protenix/model`
- `--docker_extra_args "--shm-size=16g -v /dev/shm:/dev/shm"`
- `--infer_extra_args "..."`

## Outputs

Under `--output_dir`:

- `protenix2score_metrics.json`
- `protenix2score_tojson.log`
- `protenix2score_infer.log`
- `protenix_output/` (raw Protenix outputs)
- `<name>.json` (generated input JSON)

When `--prepare_only` is set, only JSON conversion artifacts are produced.

## CSV Aggregation

```bash
python Protenix2Score/collect_protenix2score_metrics.py \
  --pred_dir /path/to/protenix2score_out/protenix_output \
  --output_csv /path/to/protenix2score_out/metrics.csv
```
