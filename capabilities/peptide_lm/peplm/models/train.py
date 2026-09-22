"""Checkpoint loading for the Tier-1 prior (``arch: "modern"`` only)."""

from __future__ import annotations

import torch

from peplm.vocab import Vocab


def load_prior(path: str, device: str = "cpu"):
    """Load a Tier-1 prior checkpoint. Checkpoints without an explicit
    arch are rejected — inferring it from tensor shapes can silently load
    the wrong backbone. Returns (model, vocab)."""
    ckpt = torch.load(path, map_location=device, weights_only=False)
    arch = ckpt.get("arch")
    if arch == "modern":
        from peplm.models.train_modern import load_modern_prior

        return load_modern_prior(path, device)
    raise ValueError(
        f"checkpoint {path} declares no supported arch (got {arch!r}); "
        "expected 'modern'")
