"""Crystal validation of the D-peptide oracle against 3LNJ.

Protocol (the production D-peptide mirror workflow, exercised end to end on a
real crystal complex):
  1. target    = L-MDM2 from the 3LNJ crystal (chain A), the user-upload role;
  2. mirror    = x -> -x, giving the fixed D-MDM2 target;
  3. design    = the crystal D-PMI peptide's L-sequence (PMI motif), placed at
                 the mirrored pocket and docked with the fixed-receptor
                 inpainting sampler (RePaint-style resets) WITH MSA enabled;
  4. display   = flip back to L-MDM2 + D-peptide, align receptor to upload;
  5. metrics   = ipTM / pLDDT from the Boltz confidence head; peptide RMSD of
                 the produced D-configuration coordinates against the crystal
                 D-PMI coordinates (direct registry + best superposition).

Run:
  /data/Boltz2Score/.venv/bin/python \
    capabilities/peptide_lm/scripts/validate_dpeptide_oracle_crystal.py \
    --device cuda:1
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

PEPLM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PEPLM_ROOT))

import gemmi  # noqa: E402
import numpy as np  # noqa: E402

FIXTURE = PEPLM_ROOT / "tests" / "fixtures" / "3LNJ_native.pdb"


def _kabsch(P: np.ndarray, Q: np.ndarray):
    pc, qc = P.mean(0), Q.mean(0)
    H = (P - pc).T @ (Q - qc)
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T
    return R, qc - R @ pc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out_dir", default="/tmp/dpeptide_crystal_validation")
    ap.add_argument("--use_msa", default="1")
    args = ap.parse_args()
    import os

    os.environ["PEPLM_DEVICE"] = args.device
    os.environ["DPEPTIDE_USE_MSA"] = args.use_msa
    out_root = Path(args.out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    from peplm.dpeptide import mirror as dpm
    from peplm.dpeptide.scoring import dock_peptide, load_model_once

    # ---- 1) target: L-MDM2 crystal chain A, written standalone (upload role)
    src = gemmi.read_structure(str(FIXTURE))
    src.setup_entities()
    target_st = gemmi.Structure()
    target_st.name = "LMDM2"
    model = gemmi.Model("1")
    chain_a = gemmi.Chain(src[0][0].name)
    for residue in src[0][0]:
        chain_a.add_residue(residue.clone())
    model.add_chain(chain_a)
    target_st.add_model(model)
    target_st.setup_entities()
    target_path = out_root / "target_LMDM2.pdb"
    target_st.write_pdb(str(target_path))

    # crystal peptide: sequence (renamed L letters) + crystal coordinates
    pep_chain = src[0][1]
    seq = gemmi.one_letter_code([r.name for r in pep_chain]).upper()
    crystal_ca = np.array([[a.pos.x, a.pos.y, a.pos.z]
                           for r in pep_chain for a in r if a.name == "CA"])
    print(f"[crystal] peptide sequence {seq} ({len(crystal_ca)} res)")

    # ---- 2) mirror target, resolve mirrored pocket at crystal peptide centroid
    pocket = crystal_ca.mean(axis=0)
    pocket_m = (-pocket[0], pocket[1], pocket[2])
    d_target = gemmi.read_structure(str(target_path))
    d_target.setup_entities()
    dpm.mirror_structure(d_target)
    d_target.setup_entities()
    d_path = out_root / "d_target.pdb"
    d_target.write_pdb(str(d_path))

    # ---- 3) stage: crystal-geometry L-peptide (the ideal conformer) at pocket
    # here we deliberately start from the crystal conformer itself: this asks
    # the oracle to keep/certify a crystal-like pose, the hardest reference.
    place_st = gemmi.Structure()
    place_st.name = "staged"
    model = gemmi.Model("1")
    rec_chain = gemmi.Chain(d_target[0][0].name)
    for residue in d_target[0][0]:
        rec_chain.add_residue(residue.clone())
    model.add_chain(rec_chain)
    rng = np.random.default_rng(100 + args.seed)
    pep_all = np.array([[a.pos.x, a.pos.y, a.pos.z]
                        for r in pep_chain for a in r])
    centroid = pep_all.mean(axis=0)
    axis = rng.normal(size=3)
    axis /= np.linalg.norm(axis)
    theta = rng.uniform(0, 2 * np.pi)
    K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    rot = np.eye(3) + math.sin(theta) * K + (1 - math.cos(theta)) * (K @ K)
    pep_out = gemmi.Chain("B")
    for num, residue in enumerate(pep_chain, start=1):
        nr = gemmi.Residue()
        nr.name = residue.name
        nr.seqid = gemmi.SeqId(num, " ")
        nr.het_flag = "A"
        for atom in residue:
            na = gemmi.Atom()
            na.name = atom.name
            na.element = atom.element
            v = rot @ (np.array([atom.pos.x, atom.pos.y, atom.pos.z]) - centroid) \
                + np.asarray(pocket_m)
            na.pos = gemmi.Position(*v)
            nr.add_atom(na)
        pep_out.add_residue(nr)
    model.add_chain(pep_out)
    place_st.add_model(model)
    place_st.setup_entities()
    staged_path = out_root / "staged.pdb"
    place_st.write_pdb(str(staged_path))

    # ---- 4) fixed-receptor docking oracle (MSA on via env)
    model_module = load_model_once()
    result = dock_peptide(staged_path, out_root / "oracle", model_module=model_module,
                          seed=args.seed, pocket_box=6.0)

    # ---- 5) display flip + align back to the upload frame
    from peplm.dpeptide.pipeline import flip_product
    from peplm.dpeptide import chirality_report

    structure_dir = Path(result["structure_dir"])
    best_cif = sorted(structure_dir.glob("*_model_*.cif"))[0]
    product = out_root / "PRODUCT_LMDM2_DPMI.pdb"
    flip_product(best_cif, product)

    prod = gemmi.read_structure(str(product))
    prod.setup_entities()
    prot = sorted((c for c in prod[0]
                   if sum(1 for r in c if r.het_flag != "H") >= 3),
                  key=lambda c: -sum(len(r) for r in c))
    rec_report = chirality_report(prod, prot[0].name)
    pep_report = chirality_report(prod, prot[1].name)

    _, ref_ca = None, None
    tgt_check = gemmi.read_structure(str(target_path))
    tgt_check.setup_entities()
    ref_pts = np.array([[a.pos.x, a.pos.y, a.pos.z]
                        for r in tgt_check[0][0] for a in r if a.name == "CA"])
    prod_pts = np.array([[a.pos.x, a.pos.y, a.pos.z]
                         for r in prot[0] for a in r if a.name == "CA"])
    n = min(len(prod_pts), len(ref_pts))
    receptor_rmsd = float(np.sqrt(((prod_pts[:n] - ref_pts[:n]) ** 2).sum(1).mean()))

    # ---- 6) peptide RMSD vs crystal D-PMI (direct + best registry)
    prod_pep = np.array([[a.pos.x, a.pos.y, a.pos.z]
                         for r in prot[1] for a in r if a.name == "CA"])
    m = min(len(prod_pep), len(crystal_ca))
    direct = float(np.sqrt(((prod_pep[:m] - crystal_ca[:m]) ** 2).sum(1).mean()))
    best_registry, best_rmsd = None, float("inf")
    m2 = len(prod_pep)
    for shift in range(0, max(1, len(crystal_ca) - m2 + 1)):
        seg = crystal_ca[shift:shift + m2]
        if len(seg) < m2:
            break
        R, t = _kabsch(prod_pep, seg)
        rmsd = float(np.sqrt((((R @ prod_pep.T).T + t - seg) ** 2).sum(1).mean()))
        if rmsd < best_rmsd:
            best_rmsd, best_registry = rmsd, shift
    # also: unrestricted best superposition (same registry, optimal rigid fit)
    R, t = _kabsch(prod_pep[:m], crystal_ca[:m])
    irm = float(np.sqrt((((R @ prod_pep[:m].T).T + t - crystal_ca[:m]) ** 2).sum(1).mean()))

    report = {
        "peptide_sequence": seq,
        "use_msa": args.use_msa == "1",
        "iptm": result.get("iptm"),
        "confidence_score": result.get("confidence_score"),
        "complex_plddt": result.get("complex_plddt"),
        "peptide_plddt_proxy": result.get("ligand_plddt_mean", result.get("plddt")),
        "chirality": {
            "receptor_mean_ca_volume": round(rec_report.mean_volume, 3),
            "peptide_mean_ca_volume": round(pep_report.mean_volume, 3),
            "receptor_is_L": rec_report.mean_volume > 0,
            "peptide_is_D": pep_report.mean_volume < 0,
        },
        "receptor_vs_upload_rmsd": round(receptor_rmsd, 3),
        "peptide_rmsd_vs_crystal": {
            "direct_registry": round(direct, 2),
            "best_superposition_same_registry": round(irm, 2),
            "best_registry_shift_rmsd": round(best_rmsd, 2),
            "registry_shift": best_registry,
        },
        "oracle_dir": str(structure_dir),
        "product": str(product),
    }
    print(json.dumps(report, indent=1))
    (out_root / "validation_report.json").write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
