"""Consistency guard: cross-backend self-consistency for the top-k.

Wraps the primary oracle. After each round's scoring, the top-k candidates
(by current reward, oracle-scored preferred) are re-folded with a SECOND,
independent predictor (boltz <-> protenix). The two interchain PAE
submatrices are compared (interchain_pae.consistency_score) and the result
is merged into each candidate's metrics:

    self_consistency, corr, d_min_ipae

If the secondary fold fails (any exception), the candidate keeps its primary
metrics and the reward term is simply skipped — a guard, never a blocker.
No RMSD, no multi-sample consensus: exactly one extra prediction for at most
top-k candidates per round.
"""

from __future__ import annotations

from peplm.candidate import Candidate
from peplm.oracle.interchain_pae import (
    consistency_score,
    extract_boltz_pae,
    extract_protenix_pae,
)


class ConsistencyGuard:
    def __init__(self, primary, secondary, topk: int = 8, log=print):
        self.primary = primary
        self.secondary = secondary
        self.topk = max(0, int(topk))
        self.log = log
        self.n_extra = 0
        self.n_ok = 0

    @property
    def n_calls(self) -> int:
        return getattr(self.primary, "n_calls", 0)

    @property
    def wall_s(self) -> float:
        return getattr(self.primary, "wall_s", 0.0)

    # ------------------------------------------------------------ interface
    def score(self, candidates: list[Candidate], tag: str = "b") -> list[Candidate]:
        self.primary.score(candidates, tag=tag)
        if self.topk <= 0 or self.secondary is None or not candidates:
            return candidates
        scored = [c for c in candidates if c.metrics.get("pair_iptm") is not None]
        if not scored:
            return candidates

        def _rank(c):
            m = c.metrics
            # the guard runs BEFORE the round's rewards are computed: rank by
            # the already-scored confidence (composite > interface > pair)
            for k in ("composite", "best", "pair_iptm"):
                v = m.get(k)
                if v is not None:
                    return float(v)
            return 0.0

        ranked = sorted(scored, key=_rank, reverse=True)
        top = ranked[: self.topk]
        if not top:
            return candidates
        self.n_extra += len(top)
        self.secondary.score(top, tag=f"{tag}_sc")
        for c in top:
            pae_a = self._extract_primary(c)
            pae_b = self._extract_secondary(c)
            if pae_a is None or pae_b is None:
                continue
            try:
                res = consistency_score(pae_a, pae_b)
            except Exception as e:
                self.log(f"[consistency] score failed: {e}")
                continue
            c.metrics.update(res)
            self.n_ok += 1
        return candidates

    # ------------------------------------------------------------ extraction
    def _extract_primary(self, cand: Candidate):
        return _extract_from_metrics(cand, prefer="record_dir")

    def _extract_secondary(self, cand: Candidate):
        # the secondary fold wrote protenix_pred_root; record_dir still holds
        # the PRIMARY boltz path and must not shadow it
        return _extract_from_metrics(cand, prefer="protenix_pred_root")


def _extract_from_metrics(cand: Candidate, prefer: str | None = None):
    """Best-effort: the oracle already stored row-level interchain PAE where
    the backend exposes it; falls back to re-reading the record dirs."""
    m = cand.metrics
    n = len(cand.residues)
    # protenix keeps token-level data accessible via its run dir; boltz via
    # its npz. Prefer the cheapest: parse from the recorded dirs if the
    # oracle recorded them, else compute from cached paths.
    from peplm.oracle.interchain_pae import InterchainPAE

    mat = m.get("_ipsae_submatrix")
    if mat is not None:
        import numpy as np

        arr = np.asarray(mat)
        out = InterchainPAE(matrix=arr, min_ipae=float(m.get("min_ipae", arr.min())),
                            mean_ipae=float(m.get("mean_ipae", arr.mean())),
                            n_binder=n)
        return out
    # oracle-recorded record dir (boltz)
    from pathlib import Path as P

    order = ["protenix_pred_root", "record_dir"] if prefer == "protenix_pred_root" \
        else ["record_dir", "protenix_pred_root"]
    for key in order:
        if key == "record_dir" and m.get("record_dir"):
            out = extract_boltz_pae(P(m["record_dir"]), n)
            if out is not None:
                return out
        if key == "protenix_pred_root" and m.get("protenix_pred_root"):
            out = extract_protenix_pae(P(m["protenix_pred_root"]), n)
            if out is not None:
                return out
    return None