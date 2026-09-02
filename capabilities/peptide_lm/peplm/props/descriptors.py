"""Unified peptide property descriptors.

compute_props() is the single entry point used by corpus tagging, the reward
function and candidate filters. All terms are pure-python microsecond-scale —
no model calls — and deliberately transparent (documented constants).
"""

from __future__ import annotations

from peplm.props.charge import net_charge
from peplm.props.liability import liability_score
from peplm.props.solubility import AROMATIC, solubility_score
from peplm.props.synthesizability import synthesizability_score
from peplm.residues import (
    BETA_RESIDUES,
    D_RESIDUES,
    HYDROPATHY,
    NCAA_TOKENS,
    residue_masses,
)
from peplm.vocab import residue_tokens

HYDROPHOBIC = set("AILMFWYV")


def molecular_weight(tokens: list[str], cyclic: bool = False) -> float:
    masses = residue_masses()
    total = sum(masses.get(t, 110.0) for t in tokens)
    if not cyclic:
        total += 18.010565  # condensation leaves one H2O on the chain
    return total


def hydropathy_ratio(tokens: list[str]) -> float:
    """Mean Kyte-Doolittle hydropathy normalized to [0,1] (0=fully polar)."""
    if not tokens:
        return 0.5
    total = sum(HYDROPATHY.get(t, 0.0) for t in tokens)
    return min(1.0, max(0.0, (total / len(tokens) + 4.5) / 9.0))


# calibrated by build_corpus against the empirical distribution of chopped
# UniRef windows (percentile cuts, not absolute magic numbers) — see
# build_corpus._calibrate_thresholds; these defaults match a typical corpus
DEV_HI_CUT = 0.62
DEV_MD_CUT = 0.50


def _dev_class(sol: float, syn: float, liab: float,
               hi_cut: float = DEV_HI_CUT, md_cut: float = DEV_MD_CUT) -> str:
    """Composite developability tag: geometric mean of the three pillars."""
    dev = (max(sol, 1e-3) * max(syn, 1e-3) * max(liab, 1e-3)) ** (1 / 3)
    if dev >= hi_cut:
        return "hi"
    if dev >= md_cut:
        return "md"
    return "lo"


def compute_props(tokens: list[str], cyclic: bool = False) -> dict:
    """All descriptors for one candidate (residue tokens in, scores out)."""
    res = residue_tokens(tokens)
    n = max(len(res), 1)
    sol = solubility_score(res, cyclic=cyclic)
    syn = synthesizability_score(res, cyclic=cyclic)
    liab = liability_score(res)
    ncaa = sum(1 for t in res if t in NCAA_TOKENS)
    hydrophobic_ratio = sum(1 for t in res if t in HYDROPHOBIC) / n
    aromatic_ratio = sum(1 for t in res if t in AROMATIC) / n
    charge = net_charge(res)
    dev = (sol * syn * liab) ** (1 / 3)
    return {
        "solubility": sol,
        "synthesizability": syn,
        "liability": liab,
        "developability": dev,
        "dev_class": _dev_class(sol, syn, liab),
        "net_charge": round(charge, 3),
        "mw": round(molecular_weight(res, cyclic=cyclic), 1),
        "length": len(res),
        "ncaa_count": ncaa,
        "d_residue_count": sum(1 for t in res if t in D_RESIDUES),
        "beta_residue_count": sum(1 for t in res if t in BETA_RESIDUES),
        "hydrophobic_ratio": round(hydrophobic_ratio, 3),
        "aromatic_ratio": round(aromatic_ratio, 3),
        "hydropathy": round(hydropathy_ratio(res), 3),
        "cyclic": bool(cyclic),
    }


def dev_tag_for(tokens: list[str], cyclic: bool = False,
                hi_cut: float = DEV_HI_CUT, md_cut: float = DEV_MD_CUT) -> str:
    p = compute_props(tokens, cyclic=cyclic)
    return f"<dev_{_dev_class(p['solubility'], p['synthesizability'], p['liability'], hi_cut, md_cut)}>"
