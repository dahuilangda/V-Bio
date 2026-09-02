"""Chain-pair ipSAE for peptide-protein complexes.

Same math as the production metrics.ligand_ipsae (ptm_func on the PAE of
interface pairs, d0 from interface size), generalized to two arbitrary chains:
the binder is a protein chain, not a NONPOLYMER ligand, so the production
ligand-aware module cannot classify it. Token order follows the input YAML
(target chain first, binder second), matching boltz's PAE/plddt token layout.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


def parse_cif_ca(cif_path: Path) -> list[tuple[str, np.ndarray]]:
    """(auth chain, CA coordinate) per residue, in file order."""
    lines = Path(cif_path).read_text().splitlines()
    cols: dict[str, int] = {}
    out: list[tuple[str, np.ndarray]] = []
    in_loop = False
    for ln in lines:
        s = ln.strip()
        if s == "loop_":
            in_loop, cols = True, {}
            continue
        if in_loop and s.startswith("_atom_site."):
            cols[s.split(".", 1)[1].split()[0]] = len(cols)
            continue
        if in_loop and cols and s.startswith("ATOM"):
            p = s.split()
            try:
                if p[cols["label_atom_id"]] != "CA":
                    continue
                chain = p[cols["auth_asym_id"]]
                xyz = np.array([float(p[cols["Cartn_x"]]),
                                float(p[cols["Cartn_y"]]),
                                float(p[cols["Cartn_z"]])])
            except (KeyError, ValueError, IndexError):
                continue
            out.append((chain, xyz))
        elif in_loop and cols and s == "#":
            in_loop = False
    return out


def _ptm(x: np.ndarray, d0: float) -> np.ndarray:
    return 1.0 / (1.0 + (x / d0) ** 2.0)


def _d0(length: int) -> float:
    length = max(int(length), 1)
    if length > 27:
        return max(1.0, 1.24 * (float(length) - 15.0) ** (1.0 / 3.0) - 1.8)
    return 1.0


def chain_pair_ipsae(cif_path: Path, pae_npz: Path, binder_chain: str = "B",
                     pae_cutoff: float = 12.0,
                     dist_cutoff: float = 10.0) -> dict:
    """CA-CA 10 A is the residue-contact proxy (side-chain contact implies
    CA-CA < ~10 A); the production 5 A cutoff applies to atom-level ligand
    tokens and yields zero pairs for CA-level chain pairs."""
    ca = parse_cif_ca(cif_path)
    if not ca:
        raise ValueError("no CA atoms parsed")
    pae = np.load(pae_npz)["pae"]
    binder_idx = np.array([i for i, (c, _) in enumerate(ca) if c == binder_chain], int)
    target_idx = np.array([i for i, (c, _) in enumerate(ca) if c != binder_chain], int)
    if not len(binder_idx) or not len(target_idx):
        raise ValueError("empty chain split for ipSAE")
    # protein CA tokens may be fewer than pae.shape[0] when non-protein
    # tokens (bicyclic linker ligand) are present at the tail — the
    # protein-pair submatrix is unaffected
    if len(ca) > pae.shape[0]:
        raise ValueError(f"token mismatch {len(ca)} vs {pae.shape[0]}")
    t_xyz = np.stack([ca[i][1] for i in target_idx])
    b_xyz = np.stack([ca[i][1] for i in binder_idx])
    d = np.sqrt(((t_xyz[:, None] - b_xyz[None]) ** 2).sum(-1))
    pae_tb = pae[np.ix_(target_idx, binder_idx)]
    valid = (d <= dist_cutoff) & (pae_tb < pae_cutoff)
    if not valid.any():
        return {"ipsae_dom": 0.0, "ligand_ipsae_max": 0.0,
                "interface_pairs": 0}
    n0 = int(valid.any(1).sum() + valid.any(0).sum())
    ipsae_dom = float(_ptm(pae_tb[valid], _d0(n0)).mean())
    # best single binder-residue interface score (production ligand_ipsae_max)
    best = 0.0
    for j in range(valid.shape[1]):
        m = valid[:, j]
        if m.any():
            best = max(best, float(_ptm(pae_tb[m, j], _d0(int(m.sum()))).mean()))
    return {"ipsae_dom": ipsae_dom, "ligand_ipsae_max": best,
            "interface_pairs": int(valid.sum()),
            # SOTA interchain confidence terms (Latent-X / AlphaProteo):
            # min/mean over ALL target x binder PAE entries (no distance gate)
            "min_ipae": float(pae_tb.min()),
            "mean_ipae": float(pae_tb.mean())}
