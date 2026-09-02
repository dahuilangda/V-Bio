"""Interchain PAE extraction + cross-fold self-consistency (no RMSD).

Upgrade-1 core: instead of comparing coordinates (complex RMSD needs a
superposition that flexible targets break), compare the two predictions'
interchain PAE submatrices (target x binder) — alignment-free and
interpretable:

  self_consistency = corr(P_A, P_B)  x  1/(1 + exp((|min_ipae_A - min_ipae_B| - 2)/1))

Both folds run the SAME candidate through two independent predictors
(boltz <-> protenix via the consistency guard); high correlation + small
min-ipae delta means the second predictor saw the same interface — the
single-model self-confirmation loop is broken without coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class InterchainPAE:
    matrix: np.ndarray       # [n_target, n_binder] PAE in Angstrom
    min_ipae: float
    mean_ipae: float
    n_binder: int

    def sub(self, max_len: int = 512):
        """Cap matrix size for the correlation (cheap; tail residues do not
        change interface statistics)."""
        return self.matrix[:max_len, :max_len]


# ------------------------------------------------------------------ boltz
def extract_boltz_pae(record_dir: Path, n_binder: int,
                      mi: int | None = None) -> InterchainPAE | None:
    """From a boltz prediction record (pae npz + token layout: target first,
    then binder; the linker ligand tokens, when present, come last)."""
    try:
        import numpy as np

        from peplm.oracle.peptide_boltz import _pick_best_model

        record_dir = Path(record_dir)
        if mi is None:
            mi = _pick_best_model(record_dir)
        if mi is None:
            return None
        stem = record_dir.name
        pae_path = record_dir / f"pae_{stem}_model_{mi}.npz"
        if not pae_path.exists():
            return None
        pae = np.load(pae_path)["pae"]
        n_t = pae.shape[0] - n_binder
        if n_t <= 0 or n_binder <= 0:
            return None
        m = pae[:n_t, :n_binder]
        return InterchainPAE(matrix=m, min_ipae=float(m.min()),
                             mean_ipae=float(m.mean()), n_binder=n_binder)
    except Exception:
        return None


# ---------------------------------------------------------------- protenix
def extract_protenix_pae(pred_root: Path, binder_residues: int,
                         samples: bool = False) -> InterchainPAE | None:
    """From a protenix predictions dir (full-data json token arrays)."""
    try:
        import json

        import numpy as np

        summaries = sorted(pred_root.glob("*_summary_confidence_sample_*.json"))
        if not summaries:
            return None
        best = None
        best_rs = -1e9
        for p in summaries:
            s = json.loads(p.read_text())
            rs = s.get("ranking_score")
            if rs is not None and float(rs) > best_rs:
                best_rs, best = float(rs), p
        if best is None:
            return None
        import re

        m = re.search(r"_summary_confidence_sample_(\d+)\.json$", best.name)
        idx = m.group(1)
        stem = best.name[: best.name.rfind(f"_summary_confidence_sample_{idx}.json")]
        fd = pred_root / f"{stem}_full_data_sample_{idx}.json"
        if not fd.exists():
            return None
        d = json.loads(fd.read_text())
        asym = np.asarray(d.get("token_asym_id") or [])
        pae = d.get("token_pair_pae")
        if len(asym) == 0 or not isinstance(pae, list) or len(pae) != len(asym):
            return None
        pae_m = np.asarray(pae, dtype=float)
        groups: dict[int, list[int]] = {}
        for t_i, a in enumerate(asym):
            groups.setdefault(int(a), []).append(t_i)
        binder_asym = next((a for a, ix in groups.items()
                            if len(ix) == binder_residues), None)
        if binder_asym is None:
            by_size = sorted(groups.items(), key=lambda kv: len(kv[1]))
            binder_asym = by_size[1][0] if len(by_size) > 1 else None
        if binder_asym is None:
            return None
        target_asym = max(groups, key=lambda a: len(groups[a]))
        ti = np.asarray(groups[target_asym])
        bi = np.asarray(groups[binder_asym])
        sub = pae_m[np.ix_(ti, bi)]
        return InterchainPAE(matrix=sub, min_ipae=float(sub.min()),
                             mean_ipae=float(sub.mean()),
                             n_binder=len(bi))
    except Exception:
        return None


# ------------------------------------------------------------- consistency
def consistency_score(a: InterchainPAE, b: InterchainPAE) -> dict:
    """corr of the two submatrices (interpolated to a common shape for the
    adaptive-length case) + min-ipae delta penalty."""
    nr = min(a.matrix.shape[0], b.matrix.shape[0])
    nc = min(a.matrix.shape[1], b.matrix.shape[1])
    ma, mb = a.matrix[:nr, :nc], b.matrix[:nr, :nc]
    r = float(np.corrcoef(ma.ravel(), mb.ravel())[0, 1])
    if not np.isfinite(r):
        r = 0.0
    d_min = abs(float(ma.min()) - float(mb.min()))
    # delta penalty: ~1 at |d|<2 A, ~0.5 at 4 A, ->0 at 8 A
    delta_term = 1.0 / (1.0 + np.exp((d_min - 2.0) / 1.0))
    corr_term = max(0.0, min(1.0, (r + 1.0) / 2.0))  # [-1,1] -> [0,1]
    return {
        "corr": round(r, 4),
        "d_min_ipae": round(float(d_min), 4),
        "self_consistency": round(float(corr_term * delta_term), 4),
    }