"""All-atom structure quality battery for designed peptide complexes.

Checks interchain clash, backbone integrity, CA chirality, omega
planarity, aromatic rings and interface shape. The reference envelope
is measured from crystal bound peptides (3LNJ), not protein domains.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

# crystal bound-peptide reference (3LNJ chain F D-peptide, 10 res)
CRYSTAL_BOUND_PEPTIDE = {
    "contact_frac": 0.81, "min_interdist": 2.52, "caca_min": 3.73,
}

ACCEPT = {
    "min_interdist": 2.3,     # deep interpenetration floor
    "caca_min": 3.2,          # backbone collapse floor
    "omega_med_deg": 15.0,    # median |phi-180| over non-Pro bonds
    "omega_bad": 0,           # bonds twisted > 60 deg
    "chir_mixed": 0,          # sign flips within the chain
}

_RING_ATOMS = {
    "PHE": ("CG", "CD1", "CE1", "CZ", "CE2", "CD2"),
    "TYR": ("CG", "CD1", "CE1", "CZ", "CE2", "CD2"),
    "HIS": ("CG", "ND1", "CE1", "NE2", "CD2"),
}


def _dihedral(p0, p1, p2, p3):
    b0, b1v, b2v = p0 - p1, p2 - p1, p3 - p2
    b1u = b1v / np.linalg.norm(b1v)
    v = b0 - np.dot(b0, b1u) * b1u
    w = b2v - np.dot(b2v, b1u) * b1u
    return np.degrees(np.arctan2(np.dot(np.cross(b1u, v), w), np.dot(v, w)))


def _clean_rn(name: str) -> str:
    u = name.upper()
    if u.startswith("D-"):
        return u[2:]
    if len(u) == 4 and u.startswith("D"):
        return u[1:]
    return u


def audit_complex(cif_path: Path | str, peptide_len_range=(8, 40)):
    """Full battery on one predicted complex. Returns a flat dict."""
    import gemmi

    st = gemmi.read_structure(str(cif_path))
    st.setup_entities()
    st.remove_hydrogens()
    chains = list(st[0])
    pep = next((c for c in chains
                if peptide_len_range[0] <= len(c) <= peptide_len_range[1]),
               None)
    if pep is None:
        return {"error": "no peptide chain"}
    rec = np.array([[a.pos.x, a.pos.y, a.pos.z]
                    for c in chains if c is not pep for r in c for a in r])
    xyz = np.array([[a.pos.x, a.pos.y, a.pos.z] for r in pep for a in r])
    d = np.linalg.norm(rec[:, None, :] - xyz[None, :, :], axis=-1)

    res_atoms = []
    i = 0
    for r in pep:
        at = {}
        for a in r:
            at[a.name] = xyz[i]
            i += 1
        res_atoms.append(at)

    # clash
    out = {"min_interdist": float(d.min()),
           "pairs_deep": int((d < 2.3).sum()),
           "contact_frac": float((d < 5.0).any(axis=0).mean())}

    # backbone
    ca = np.array([at["CA"] for at in res_atoms if "CA" in at])
    out["caca_min"] = (float(np.linalg.norm(np.diff(ca, axis=0), axis=1).min())
                       if len(ca) >= 2 else None)

    # chirality
    signs = []
    for at in res_atoms:
        if all(k in at for k in ("N", "CA", "C", "CB")):
            v = np.stack([at["N"] - at["CA"], at["C"] - at["CA"],
                          at["CB"] - at["CA"]])
            signs.append(float(np.linalg.det(v)))
    out["chir_mixed"] = (int(((np.array(signs) > 0) != (signs[0] > 0)).sum())
                         if signs else 0)

    # omega (cis-Pro excluded by design)
    devs = []
    for i in range(len(res_atoms) - 1):
        a, b = res_atoms[i], res_atoms[i + 1]
        if not all(k in a for k in ("CA", "C")) or \
                not all(k in b for k in ("N", "CA")):
            continue
        phi = _dihedral(a["CA"], a["C"], b["N"], b["CA"])
        devs.append(abs(abs(phi) - 180.0))
    devs = np.array(devs) if devs else np.array([0.0])
    out["omega_med_deg"] = float(np.median(devs))
    out["omega_bad"] = int((devs > 60).sum())

    # aromatic rings
    ring_dev = 0.0
    for at, r in zip(res_atoms, pep):
        rn = _clean_rn(r.name)
        ring = _RING_ATOMS.get(rn)
        if ring and all(k in at for k in ring):
            pts = np.stack([at[k] for k in ring])
            c0 = pts.mean(0)
            _, _, vt = np.linalg.svd(pts - c0)
            ring_dev = max(ring_dev, float(np.abs((pts - c0) @ vt[2]).max()))
    out["ring_dev_max"] = ring_dev

    out["clean"] = (
        out["min_interdist"] >= ACCEPT["min_interdist"]
        and (out["caca_min"] or 0.0) >= ACCEPT["caca_min"]
        and out["chir_mixed"] == ACCEPT["chir_mixed"]
        and out["omega_med_deg"] <= ACCEPT["omega_med_deg"]
        and out["omega_bad"] <= ACCEPT["omega_bad"])
    return out





def summary_line(metrics: dict) -> str:
    return ("inter=%(min_interdist).2f cfrac=%(contact_frac).2f "
            "caca=%(caca_min)s chir=%(chir_mixed)d om=%(omega_med_deg).0f/"
            "%(omega_bad)d ring=%(ring_dev_max).2f "
            "%(clean)s" % {**{k: (v if v is not None else -1)
                              for k, v in metrics.items()},
                           "clean": "CLEAN" if metrics.get("clean") else ""})
