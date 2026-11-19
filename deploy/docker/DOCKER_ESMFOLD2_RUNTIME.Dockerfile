# ESMFold2 runtime: python 3.12 + torch >= 2.11 (upgraded from the mol1 base)
# + the esm package + RDKit. Weights (biohub/ESMFold2) download on first run
# into the mounted HF cache (/data/esmfold2/hf_cache on the host). The
# capability script is read through the /data/V-Bio mount at run time.
# Build with proxy build-args when PyPI needs it:
#   docker build --build-arg HTTP_PROXY=... --build-arg HTTPS_PROXY=...
FROM vbio-mol1-runtime:1.0.3

ARG HTTP_PROXY
ARG HTTPS_PROXY
ENV HTTP_PROXY=${HTTP_PROXY} HTTPS_PROXY=${HTTPS_PROXY}

RUN python3.12 -m pip install --no-cache-dir --upgrade "torch>=2.11.0" \
    && python3.12 -m pip install --no-cache-dir "esm>=3.4.1"

ENV HTTP_PROXY= HTTPS_PROXY=
