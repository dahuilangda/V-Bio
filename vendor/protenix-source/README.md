# Protenix v2 source (vendored)

Vendored runtime subset of [bytedance/Protenix](https://github.com/bytedance/Protenix) v2,
mounted into the `vbio-protenix-v2-runtime` container at `/app` and executed as
`runner/inference.py`. Only the inference runtime is included (`protenix/`, `runner/`,
`configs/`, `setup.py`, `requirements.txt`, `LICENSE`) — `assets/`, `examples/`, `docs/`,
`scripts/`, `tests/` are omitted (README images / dev tooling, not needed at inference time).
Model weights and the CCD common cache stay external (`PROTENIX_MODEL_DIR`,
`PROTENIX_COMMON_CACHE_DIR`).

## Why vendored (not a submodule)

V-Bio patches `runner/inference.py` with an opt-in low-VRAM mode. Vendoring (rather than a
submodule + patch) keeps the exact source the engine runs under version control, with no
"apply patch" deploy step — the same pattern used for the vendored molstar.

## V-Bio patch

`runner/inference.py` → `update_inference_configs()`: when env `PROTENIX_LOW_VRAM=1` is set,
force `sample_diffusion_chunk_size=1`, aggressive `chunk_size_thresholds`, `msa_chunk_size=512`,
and relax the `protenix-v2` token wall 2560 → 3072 (LMI4Boltz-style aggressive chunking).
Default behavior is unchanged when the env is unset. The V-Bio backend sets this env when the
user enables "Low VRAM" on a Protenix prediction.

## Deploy

`PROTENIX_SOURCE_DIR` (deploy env, e.g. `deploy/docker/DOCKER_STACK_WORKER_GPU_CAPS.env`)
points here:
```
PROTENIX_SOURCE_DIR=/data/V-Bio/vendor/protenix-source
```
The backend mounts this host path into the engine container at `/app`.

## Updating from upstream

1. Pull the desired Protenix v2 revision into a scratch checkout.
2. Re-copy `protenix/`, `runner/`, `configs/`, `setup.py`, `requirements.txt`, `LICENSE`
   over this directory (drop `__pycache__`).
3. Re-apply the `PROTENIX_LOW_VRAM` patch to `runner/inference.py: update_inference_configs`
   (see the `low_vram` / `PROTENIX_LOW_VRAM` block).
4. Run a Protenix prediction with Low VRAM on to confirm the patch fires.
