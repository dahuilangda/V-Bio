"""Interface metrics for the peptide design oracle (Protenix full_data json).

ipSAE is the reporting metric; interface PAE is the training signal, since
ipSAE collapses to ~0 for every cold-start candidate. Per-residue vectors
drive re-masking credit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


def _calc_d0(n: np.ndarray | float, min_value: float = 1.0) -> np.ndarray:
    """AlphaFold pTM d0 with Dunbrack's floor: L clamped to 27, result at
    least ``min_value``."""
    n = np.maximum(np.asarray(n, dtype=float), 27.0)
    return np.maximum(1.24 * np.cbrt(n - 15.0) - 1.8, min_value)


@dataclass
class InterfaceMetrics:
    """Scalars plus the per-peptide-residue vectors used for credit."""

    ipsae: float = 0.0                    # max over both directions
    ipsae_pep_to_rec: float = 0.0
    ipsae_rec_to_pep: float = 0.0
    min_interface_pae: float = 32.0
    mean_interface_pae: float = 32.0
    contact_mass: float = 0.0             # summed cross-chain contact prob
    binder_plddt: float = 0.0             # mean, 0-100
    lowq_plddt: float = 0.0               # lowest-quartile mean, 0-100
    per_residue_plddt: list[float] = field(default_factory=list)
    per_residue_ipae: list[float] = field(default_factory=list)
    per_residue_contact: list[float] = field(default_factory=list)
    n_peptide_tokens: int = 0

    def as_dict(self) -> dict:
        return {
            "ipsae": self.ipsae,
            "ipsae_pep_to_rec": self.ipsae_pep_to_rec,
            "ipsae_rec_to_pep": self.ipsae_rec_to_pep,
            "min_interface_pae": self.min_interface_pae,
            "mean_interface_pae": self.mean_interface_pae,
            "contact_mass": self.contact_mass,
            "binder_plddt": self.binder_plddt,
            "lowq_plddt": self.lowq_plddt,
            "per_residue_plddt": self.per_residue_plddt,
            "per_residue_ipae": self.per_residue_ipae,
            "per_residue_contact": self.per_residue_contact,
        }


def _ipsae_directional(pae: np.ndarray, src: np.ndarray, dst: np.ndarray,
                       pae_cutoff: float) -> tuple[float, np.ndarray]:
    """ipSAE for src->dst: per source residue, the pTM-style score over the
    partners it aligns confidently to. Returns (max over src, per-src row).
    d0 is per residue from that residue's own confident-partner count
    (the paper's ``d0res`` variant)."""
    if src.size == 0 or dst.size == 0:
        return 0.0, np.zeros(src.size)
    sub = pae[np.ix_(src, dst)]
    valid = sub < pae_cutoff
    n0 = valid.sum(axis=1).astype(float)
    d0 = _calc_d0(n0)
    term = np.where(valid, 1.0 / (1.0 + (sub / d0[:, None]) ** 2), 0.0)
    per_src = np.where(n0 > 0, term.sum(axis=1) / np.maximum(n0, 1.0), 0.0)
    return float(per_src.max()), per_src


def compute_interface_metrics(
    full_data_path: Path,
    peptide_len: int,
    pae_cutoff: float = 10.0,
) -> InterfaceMetrics | None:
    """Parse one Protenix ``full_data`` json into interface metrics.

    The peptide is the LAST asym (staging writes receptor first, peptide
    last) and its token count must equal ``peptide_len`` exactly; anything
    else returns None so staging/caller bugs surface instead of being
    guessed away.
    """
    if peptide_len <= 0:
        return None
    data = json.loads(Path(full_data_path).read_text())
    pae = np.asarray(data.get("token_pair_pae") or [], dtype=float)
    asym = np.asarray(data.get("token_asym_id") or [], dtype=int)
    if pae.ndim != 2 or pae.shape[0] != pae.shape[1] or asym.size != pae.shape[0]:
        return None
    labels, counts = np.unique(asym, return_counts=True)
    if labels.size < 2:
        return None
    pep_label = labels[-1]
    if counts[-1] != peptide_len:
        return None          # staged length disagrees with the caller
    pep = np.where(asym == pep_label)[0]
    rec = np.where(asym != pep_label)[0]

    m = InterfaceMetrics(n_peptide_tokens=int(pep.size))
    m.ipsae_pep_to_rec, _ = _ipsae_directional(pae, pep, rec, pae_cutoff)
    m.ipsae_rec_to_pep, _ = _ipsae_directional(pae, rec, pep, pae_cutoff)
    m.ipsae = max(m.ipsae_pep_to_rec, m.ipsae_rec_to_pep)

    # dense interface confidence: symmetrised cross-chain PAE
    cross = np.minimum(pae[np.ix_(pep, rec)], pae[np.ix_(rec, pep)].T)
    m.min_interface_pae = float(cross.min())
    per_res_ipae = cross.min(axis=1)
    m.per_residue_ipae = [float(v) for v in per_res_ipae]
    # the interface is the confident tail: whole-matrix averaging washes
    # every candidate out to the PAE ceiling
    k = max(1, int(0.1 * cross.shape[1]))
    m.mean_interface_pae = float(np.sort(cross, axis=1)[:, :k].mean())

    # contact_probs is a trunk head: identical across a sequence's samples,
    # so it ranks sequences but not poses -- never used for per-residue credit
    contacts = np.asarray(data.get("contact_probs") or [], dtype=float)
    if contacts.shape == pae.shape:
        cm = contacts[np.ix_(pep, rec)]
        m.contact_mass = float(cm.sum())
        m.per_residue_contact = [float(v) for v in cm.sum(axis=1)]
    else:
        m.per_residue_contact = [0.0] * int(pep.size)

    plddt = _per_token_plddt(data)
    if plddt is not None and plddt.size == asym.size:
        pep_plddt = plddt[pep]
        m.per_residue_plddt = [float(v) for v in pep_plddt]
        m.binder_plddt = float(pep_plddt.mean())
        s = np.sort(pep_plddt)
        q = max(1, s.size // 4)
        m.lowq_plddt = float(s[:q].mean())
    return m


def _per_token_plddt(data: dict) -> np.ndarray | None:
    """Mean per-token pLDDT on a 0-100 scale.

    ``atom_plddt`` is a probability in [0, 1] (the 0-100 scale lives only
    in the CIF B-factor column); values above 1.5 count as already scaled.
    NaN reads as zero confidence.
    """
    atom = np.asarray(data.get("atom_plddt") or [], dtype=float)
    idx = np.asarray(data.get("atom_to_token_idx") or [], dtype=int)
    if atom.size == 0 or atom.size != idx.size:
        return None
    atom = np.nan_to_num(atom, nan=0.0, posinf=0.0, neginf=0.0)
    if idx.min() < 0:
        return None
    n_tok = int(idx.max()) + 1
    total = np.bincount(idx, weights=atom, minlength=n_tok)
    count = np.bincount(idx, minlength=n_tok).astype(float)
    per_token = total / np.maximum(count, 1.0)
    return per_token * 100.0 if float(atom.max()) <= 1.5 else per_token
