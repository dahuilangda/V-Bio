# ESMFold2 runtime: torch (CUDA), the esm package, RDKit for SMILES inputs.
# Weights (biohub/ESMFold2) download on first run into the mounted
# HF cache (/data/esmfold2/hf_cache on the host). Set HF_ENDPOINT to a
# mirror when the hub is unreachable. The capability script is read through
# the /data/V-Bio mount at run time, so the image stays code-free.
FROM pytorch/pytorch:2.7.1-cuda12.6-cudnn9-runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "esm>=3.4.1" rdkit
