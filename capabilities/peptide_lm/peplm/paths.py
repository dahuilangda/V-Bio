"""Run-output location policy for offline (non-web) peptide_lm tooling.

Web-app task results already live under backend RESULTS_BASE_DIR
(<RESULTS_BASE_DIR>/<backend>/<task_id>/, auto-cleaned). The offline CLIs
(closed loop, benchmarks) used to default into ``capabilities/peptide_lm/runs``
— inside the repository — which is how gigabytes of run archives ended up
tracked by git. These helpers point offline run outputs at a unified root
OUTSIDE the repo instead:

    VBIO_RUNS_DIR if set, else /data/vbio_runs when /data exists (the
    deployment layout), else ~/.vbio/runs on a developer machine.

The legacy in-repo runs/ directory (training inputs, cached structures,
property-head assets) is no longer tracked; those regenerate from the
training entry points (peplm.data.build_corpus, peplm.score.learned_props).
"""
from __future__ import annotations

import os
from pathlib import Path

_ENV_VAR = "VBIO_RUNS_DIR"


def run_output_root() -> Path:
    """Unified root directory for offline run outputs (never inside the repo)."""
    env_value = str(os.environ.get(_ENV_VAR) or "").strip()
    if env_value:
        return Path(env_value).expanduser()
    if Path("/data").is_dir():
        return Path("/data") / "vbio_runs"
    return Path.home() / ".vbio" / "runs"


def default_run_dir(name: str) -> Path:
    """Default run directory for a tool, namespaced under the unified root."""
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(name).strip()) or "run"
    return run_output_root() / "peptide_lm" / clean
