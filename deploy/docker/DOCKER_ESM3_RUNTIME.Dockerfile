# ESM3 peptide-LM inference service runtime.
#
# The heavy Python environment (torch, esm SDK, peft - CPython 3.12 wheels
# built against the host's glibc 2.38) is NOT installed into the image: the
# compose file bind-mounts the host's /usr/bin/python3.12 + /usr/lib/python3.12
# and the Boltz2Score venv, and runs the service with them. 24.04 base gives
# the required glibc 2.39. CUDA userland ships inside the torch wheels; the
# NVIDIA driver comes from the nvidia container runtime via `gpus: all`.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
