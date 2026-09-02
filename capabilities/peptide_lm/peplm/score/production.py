"""Production composite score (V-Bio backend, verbatim semantics).

This is the *reporting* metric for benchmarks — both arms (PeptideLM and the
GA baseline) are scored with this exact formula so numbers are comparable to
the production system.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Optional


def production_liability_penalty(sequence: str, modifications: Optional[list] = None) -> dict:
    """Port of _peptide_sequence_liability_penalty (run_single_prediction.py)."""
    seq = str(sequence or "").upper()
    length = max(1, len(seq))
    counts = Counter(seq)
    hydrophobic_ratio = sum(counts.get(aa, 0) for aa in "AILMFWYV") / length
    charged_ratio = sum(counts.get(aa, 0) for aa in "DEKRH") / length
    pro_gly_ratio = sum(counts.get(aa, 0) for aa in "PG") / length
    max_run = 1
    run = 1
    for idx in range(1, len(seq)):
        if seq[idx] == seq[idx - 1]:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 1
    repeated_triples = sum(1 for idx in range(0, max(0, len(seq) - 2))
                           if seq[idx] == seq[idx + 1] == seq[idx + 2])
    penalty = 0.0
    if hydrophobic_ratio > 0.58:
        penalty += (hydrophobic_ratio - 0.58) * 0.35
    if charged_ratio > 0.45:
        penalty += (charged_ratio - 0.45) * 0.20
    if pro_gly_ratio > 0.35:
        penalty += (pro_gly_ratio - 0.35) * 0.20
    if max_run >= 4:
        penalty += min(0.12, (max_run - 3) * 0.03)
    if repeated_triples:
        penalty += min(0.08, repeated_triples * 0.015)
    return {"penalty": min(0.25, penalty),
            "hydrophobic_ratio": hydrophobic_ratio,
            "charged_ratio": charged_ratio,
            "pro_gly_ratio": pro_gly_ratio}


def production_composite(metrics: dict, base_sequence: str) -> float | None:
    """0.58*interface + 0.22*binder + 0.12*pair_ipTM + 0.08*developability.

    interface metric preference: ipsae_dom -> ligand_ipsae_max -> pair ipTM
    (production resolve_preferred_interface_metric order)."""
    ipsae = metrics.get("ipsae_dom")
    lig_max = metrics.get("ligand_ipsae_max")
    pair_iptm = metrics.get("pair_iptm")
    if ipsae is not None:
        interface = max(0.0, min(1.0, float(ipsae)))
    elif lig_max is not None:
        interface = max(0.0, min(1.0, float(lig_max)))
    elif pair_iptm is not None:
        interface = max(0.0, min(1.0, float(pair_iptm)))
    else:
        return None
    plddt = metrics.get("binder_avg_plddt")
    binder_conf = max(0.0, min(1.0, float(plddt) / 100.0)) if plddt else 0.0
    pair_conf = max(0.0, min(1.0, float(pair_iptm))) if pair_iptm is not None else 0.0
    dev = max(0.0, 1.0 - production_liability_penalty(base_sequence)["penalty"])
    return 0.58 * interface + 0.22 * binder_conf + 0.12 * pair_conf + 0.08 * dev
