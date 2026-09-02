"""Whole-module pickle cache for the Boltz2 confidence/affinity models.

Loading a Boltz2 checkpoint the standard way costs 25-50s per model:
~15s of pure-Python module construction (even on the meta device),
~10s of random weight initialisation that is immediately overwritten,
and only ~2s of actual file reading.  Since checkpoints and model
configurations are static, the fully-built module can be pickled once
and reloaded in ~1s.  Validated bit-identical outputs (confidence and
affinity) against the standard path.

Safety design:
  - Cache files are keyed by a digest of the checkpoint identity
    (path/size/mtime) plus every constructor-affecting config value,
    so a stale cache can never be silently reused for a different
    configuration.
  - Any failure loading a cache falls back to the standard path.
  - Cache writing is atomic (temp file + os.replace).
  - Set BOLTZ2SCORE_DISABLE_MODULE_CACHE=1 to bypass entirely.

Cache files (~2 GB each) live under <cache_dir>/module_cache/ and can
be deleted freely at any time.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

import torch

CACHE_DIR_NAME = "module_cache"
DISABLE_ENV = "BOLTZ2SCORE_DISABLE_MODULE_CACHE"
MAX_ENTRIES_ENV = "BOLTZ2SCORE_MODULE_CACHE_MAX_ENTRIES"
DEFAULT_MAX_ENTRIES = 5


def _max_entries() -> int:
    try:
        value = int(os.environ.get(MAX_ENTRIES_ENV, "").strip() or DEFAULT_MAX_ENTRIES)
    except ValueError:
        value = DEFAULT_MAX_ENTRIES
    return max(0, value)


def _prune_cache_dir(cache_root: Path, keep: Path) -> None:
    """Evict oldest entries beyond the size limit (never *keep*, never non-cache files)."""
    limit = _max_entries()
    if limit <= 0:
        return
    try:
        entries = [p for p in cache_root.glob("*.pt") if p != keep]
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)  # newest first
        for stale in entries[limit - 1:]:  # keep newest limit-1 besides *keep*
            stale.unlink(missing_ok=True)
            print(f"[Info] Module cache evicted (limit {limit}): {stale.name}")
    except OSError:
        pass  # best-effort only


def module_cache_enabled() -> bool:
    """Cache is on unless BOLTZ2SCORE_DISABLE_MODULE_CACHE is set to a truthy value."""
    value = os.environ.get(DISABLE_ENV, "").strip().lower()
    return value in ("", "0", "false", "no")


def _config_digest(config: dict[str, Any], checkpoint: Path | None) -> str:
    payload: dict[str, Any] = {str(k): repr(v) for k, v in sorted(config.items())}
    if checkpoint is not None:
        stat = checkpoint.stat()
        payload["_checkpoint"] = {
            "path": str(checkpoint),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    payload["_torch"] = torch.__version__
    blob = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def load_or_build_model(
    builder: Callable[[], torch.nn.Module],
    *,
    cache_dir: Path,
    checkpoint: Path | None,
    config: dict[str, Any],
    prefix: str,
    log_tag: str,
) -> torch.nn.Module:
    """Return the model from the pickle cache when possible, else build it.

    *builder* must construct the model exactly as the standard path would.
    """
    if not module_cache_enabled():
        return builder()

    cache_root = Path(cache_dir) / CACHE_DIR_NAME
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_path = cache_root / f"{prefix}_{_config_digest(config, checkpoint)}.pt"
    except OSError as exc:
        print(f"[Warning] Module cache unavailable ({exc}); using standard load.")
        return builder()

    if cache_path.exists():
        try:
            model = torch.load(cache_path, weights_only=False)
            model.eval()
            print(f"[Info] {log_tag} loaded from module cache ({cache_path.name}).")
            return model
        except Exception as exc:  # noqa: BLE001 — any unpickle/env mismatch falls back
            print(
                f"[Warning] Module cache load failed ({type(exc).__name__}: {exc}); "
                "falling back to checkpoint load."
            )

    model = builder()

    try:
        fd, tmp_path = tempfile.mkstemp(dir=cache_root, suffix=".tmp")
        os.close(fd)
        torch.save(model, tmp_path)
        os.replace(tmp_path, cache_path)
        print(f"[Info] {log_tag} module cache written: {cache_path.name}")
        _prune_cache_dir(cache_root, keep=cache_path)
    except Exception as exc:  # noqa: BLE001 — caching is best-effort
        print(f"[Warning] Module cache write failed ({exc}); continuing without cache.")
    return model
