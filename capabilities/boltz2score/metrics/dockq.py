#!/usr/bin/env python3
"""DockQ between a reference complex and a predicted complex.

DockQ = (Fnat + 1/(1+(iRMSD/1.5)^2) + 1/(1+(LRMSD/8.5)^2)) / 3
(Basu & Wallner 2016). Heavy-atom contacts use a 5 A cutoff, interface
residues a 10 A cutoff, RMSDs over backbone atoms (N/CA/C/O).

Chain roles: one or more receptor chains plus one peptide chain per
structure. Atoms are keyed by concatenated in-chain position so the two
structures map by sequence position, not by naming conventions.

    import sys; sys.path.insert(0, "<boltz2score root>")
    from metrics.dockq import load_reference, load_model, dockq_score

    ref_r, ref_p = load_reference("ref.cif", receptor=("A", "D"), peptide="B")
    mod_r, mod_p = load_model("model.cif", receptor=("A",), peptide="B")
    dockq_score(ref_r, ref_p, mod_r, mod_p)
"""
from __future__ import annotations

import gemmi
import numpy as np

BACKBONE = ("N", "CA", "C", "O")
CONTACT_CUTOFF = 5.0
INTERFACE_CUTOFF = 10.0


def _chain_atoms(chain):
    out = []
    seqpos = 0
    last = None
    for res in chain:
        for atom in res:
            if atom.element == gemmi.Element("H"):
                continue
            if res is not last:
                seqpos += 1
                last = res
            out.append((seqpos, atom.name,
                        np.array([atom.pos.x, atom.pos.y, atom.pos.z])))
    return out


def _read(path_or_text, is_text):
    st = (gemmi.read_structure_string(path_or_text) if is_text
          else gemmi.read_structure(path_or_text))
    st.setup_entities()
    return st


def load_reference(path, receptor=("A", "D", "E", "F", "G"), peptide="B"):
    """Reference receptor chains are concatenated in the given order;
    missing letters are skipped."""
    st = _read(path, False)
    chains = {ch.name: ch for ch in st[0]}
    rec = []
    offset = 0
    for name in receptor:
        if name not in chains:
            continue
        atoms = _chain_atoms(chains[name])
        rec.extend((offset + pos, atom, p) for pos, atom, p in atoms)
        offset = max(pos for pos, _a, _p in atoms) if atoms else offset
    pep = _chain_atoms(chains[peptide]) if peptide in chains else []
    return rec, pep


def load_model(path_or_text, receptor=("A",), peptide="B", is_text=False):
    st = _read(path_or_text, is_text)
    chains = {ch.name: ch for ch in st[0]}
    rec = _chain_atoms(chains[receptor[0]]) if receptor[0] in chains else []
    pep = _chain_atoms(chains[peptide]) if peptide in chains else []
    return rec, pep


def _atom_dict(atoms):
    return {(pos, name): p for pos, name, p in atoms}


def _backbone_dict(atoms):
    return {k: p for k, p in _atom_dict(atoms).items() if k[1] in BACKBONE}


def _fit(P, Q):
    """Rigid transform (R, t) minimizing |R@P + t - Q| (Kabsch)."""
    pc, qc = P.mean(axis=0), Q.mean(axis=0)
    H = (P - pc).T @ (Q - qc)
    U, _S, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T
    return R, qc - R @ pc


def _rmsd(P, Q):
    return float(np.sqrt(((P - Q) ** 2).sum(axis=1).mean()))


def dockq_score(ref_receptor, ref_peptide, mod_receptor, mod_peptide):
    """DockQ with Fnat / iRMSD / LRMSD; None when too few shared atoms."""
    rr = _atom_dict(ref_receptor)
    rp = _atom_dict(ref_peptide)
    mr = _atom_dict(mod_receptor)
    mp = _atom_dict(mod_peptide)
    rec_keys = [k for k in rr if k in mr]
    pep_keys = [k for k in rp if k in mp]
    if len(rec_keys) < 10 or len(pep_keys) < 3:
        return None

    ref_r = np.array([rr[k] for k in rec_keys])
    mod_r = np.array([mr[k] for k in rec_keys])
    ref_p = np.array([rp[k] for k in pep_keys])
    mod_p = np.array([mp[k] for k in pep_keys])

    d_ref = np.linalg.norm(ref_r[:, None, :] - ref_p[None, :, :], axis=-1)
    native = np.argwhere(d_ref <= CONTACT_CUTOFF)
    if len(native) == 0:
        return {"fnat": 0.0, "irmsd": float("nan"), "lrmsd": float("nan"),
                "dockq": 0.0, "n_native": 0}
    d_mod = np.linalg.norm(mod_r[:, None, :] - mod_p[None, :, :], axis=-1)
    fnat = float(sum(1 for i, j in native
                     if d_mod[i, j] <= CONTACT_CUTOFF) / len(native))

    # iRMSD: interface backbone atoms (10 A across the reference interface),
    # fitted on the interface itself
    r_idx = np.array([k[0] for k in rec_keys])
    p_idx = np.array([k[0] for k in pep_keys])
    close = d_ref <= INTERFACE_CUTOFF
    int_r = set(r_idx[close.any(axis=1)].tolist())
    int_p = set(p_idx[close.any(axis=0)].tolist())
    rbb_ref, rbb_mod = _backbone_dict(ref_receptor), _backbone_dict(mod_receptor)
    pbb_ref, pbb_mod = _backbone_dict(ref_peptide), _backbone_dict(mod_peptide)
    iface_ref, iface_mod = [], []
    for src, dst, res_ids in ((rbb_ref, rbb_mod, int_r), (pbb_ref, pbb_mod, int_p)):
        for key, p in src.items():
            if key[0] in res_ids and key in dst:
                iface_ref.append(p)
                iface_mod.append(dst[key])
    if len(iface_ref) >= 4:
        R, t = _fit(np.array(iface_mod), np.array(iface_ref))
        irmsd = _rmsd((R @ np.array(iface_mod).T).T + t, np.array(iface_ref))
    else:
        irmsd = float("nan")

    # LRMSD: fit receptor backbone, measure peptide backbone
    rec_ref = np.array([rbb_ref[k] for k in rbb_ref if k in rbb_mod])
    rec_mod = np.array([rbb_mod[k] for k in rbb_ref if k in rbb_mod])
    lig_ref = np.array([pbb_ref[k] for k in pbb_ref if k in pbb_mod])
    lig_mod = np.array([pbb_mod[k] for k in pbb_ref if k in pbb_mod])
    if len(rec_ref) >= 10 and len(lig_ref) >= 3:
        R, t = _fit(rec_mod, rec_ref)
        lrmsd = _rmsd((R @ lig_mod.T).T + t, lig_ref)
    else:
        lrmsd = float("nan")

    i_term = 1.0 / (1.0 + (irmsd / 1.5) ** 2) if np.isfinite(irmsd) else 0.0
    l_term = 1.0 / (1.0 + (lrmsd / 8.5) ** 2) if np.isfinite(lrmsd) else 0.0
    return {"fnat": fnat, "irmsd": irmsd, "lrmsd": lrmsd,
            "dockq": (fnat + i_term + l_term) / 3.0,
            "n_native": int(len(native))}
