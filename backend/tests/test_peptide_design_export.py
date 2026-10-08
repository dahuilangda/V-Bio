"""Peptide-design export regression tests (2026-10-08 incident set).

Each test maps to a real shipped defect (see docs/engineering-standards.md §8):
  1. dissociated product rejection — D zips once shipped staged pre-docking
     frames: peptide 74 A from the target, presented as results
  2. bound products pass the gate at real docked distances (1.5-8 A)
  3. chain-B sequence reader used by the keep-fallback
  4. D-target staging trims incomplete terminal residues (1HL3 THR332: a
     single-N terminal residue was rebuilt by the model with L chirality and
     then failed the D gate for every candidate)
  5. flip_product preserves binding geometry (mirror-space refine -> product
     frame cannot dissociate the complex)
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import gemmi
import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PEPLM_ROOT = REPO_ROOT / "capabilities" / "peptide_lm"
if str(PEPLM_ROOT) not in sys.path:
    sys.path.insert(0, str(PEPLM_ROOT))

import backend.runtime.run_single_prediction as rsp  # noqa: E402


# ---------------------------------------------------------------- helpers

THREE = ["ALA", "ARG", "ASP", "GLU", "LYS", "SER"]
ONE = ["A", "R", "D", "E", "K", "S"]


def _atom_line(serial, atom, resname, chain, resi, x, y, z, element, hetatm=False):
    record = "HETATM" if hetatm else "ATOM"
    return (
        f"{record:6s}{serial:5d} {atom:<4} {resname:>3} {chain}{resi:4d}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2}"
    )


def _write_complex(path, *, binder_offset, binder_chain="B", linker_chain=None,
                   target_chains=("A",)):
    """Synthetic product: long target chains (41 res > 40 = receptor class), a
    peptide-size binder (8 res, offset along x), and an optional 1-residue
    HETATM SEZ-like linker chain covalently bonded distance to the binder —
    the exact configuration whose covalent bond used to neutralize the gate."""
    lines = []
    serial = 1
    target_res = ["ALA", "ARG", "ASP", "GLY"] * 11  # 44 residues > 40
    for ci, chain in enumerate(target_chains):
        for i, resname in enumerate(target_res):
            y = i * 3.8 + ci * 30.0
            for atom, (dx, dy, dz), el in (
                ("N", (1.2, 0, 0), "N"), ("CA", (0, 0, 0), "C"),
                ("C", (-1.2, 0.3, 0), "C"), ("CB", (0, 1.5, 0), "C"),
            ):
                lines.append(_atom_line(serial, atom, resname, chain, i + 1,
                                        0.0 + dx, y + dy, 0.0 + dz, el))
            serial += 1
    binder_res = ["GLY", "ALA", "SER"] * 3  # 9 residues: peptide-size
    for i, resname in enumerate(binder_res):
        for atom, (dx, dy, dz), el in (
            ("N", (1.2, 0, 0), "N"), ("CA", (0, 0, 0), "C"),
            ("C", (-1.2, 0.3, 0), "C"), ("CB", (0, 1.5, 0), "C"),
        ):
            lines.append(_atom_line(serial, atom, resname, binder_chain, i + 1,
                                    binder_offset + dx, 0.0 + dy, 5.0 + dz, el))
        serial += 1
    if linker_chain:
        # one-residue HETATM "SEZ" placed at covalent-bond distance from the
        # binder's first CA: its atoms sit ~1.8 A from binder atoms
        lines.append(_atom_line(serial, "CD", "SEZ", linker_chain, 1,
                                binder_offset + 2.0, 0.0, 5.0, "C", hetatm=True))
        serial += 1
    path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")


def _min_binder_distance(pdb_path: Path) -> float:
    st = gemmi.read_structure(str(pdb_path))
    st.setup_entities()
    chains = {}
    for ch in st[0]:
        pts = np.array([[a.pos.x, a.pos.y, a.pos.z] for r in ch for a in r
                        if a.element.name != "H"])
        if pts.shape[0]:
            chains[ch.name] = pts
    a, b = chains["A"], chains["B"]
    return float(np.linalg.norm(a[:, None, :] - b[None, :, :], axis=2).min())


# ------------------------------------------------- 1+2: bound-state gate

def test_assert_product_bound_rejects_dissociated(tmp_path):
    pdb = tmp_path / "far.pdb"
    _write_complex(pdb, binder_offset=74.0)
    assert _min_binder_distance(pdb) > 12.0
    with pytest.raises(RuntimeError, match="dissociated"):
        rsp._assert_product_bound(pdb, "B", rank=1, sequence="CREEEC")


def test_assert_product_bound_passes_real_docked_distances(tmp_path):
    for offset in (2.6, 5.0, 8.0):  # tight dock / typical / weak-but-bound
        pdb = tmp_path / f"bound_{offset}.pdb"
        _write_complex(pdb, binder_offset=offset)
        got = rsp._assert_product_bound(pdb, "B", rank=2, sequence="CREEEC")
        assert got == pytest.approx(_min_binder_distance(pdb))


def test_assert_product_bound_stale_binder_name_still_gated(tmp_path):
    """F1 regression: a stale YAML-space binder name that matches a receptor
    copy must not measure receptor-receptor contacts and wave a DISSOCIATED
    structure through — binder auto-detection (peptide-size chain) gates it."""
    pdb = tmp_path / "renamed.pdb"
    _write_complex(pdb, binder_offset=74.0, target_chains=("A", "B"),
                   binder_chain="D")
    # stale name 'B' = second receptor copy; role classification must find the
    # real binder 'D' and raise on dissociation either way.
    with pytest.raises(RuntimeError):
        rsp._assert_product_bound(pdb, "B", rank=3, sequence="CREEEC")


def test_gate_neutralized_by_covalent_linker_is_fixed(tmp_path):
    """F2 regression: a bicyclic product's SEZ linker is COVALENTLY bonded to
    the binder (~1.8 A). Counting the linker on the target side pinned min
    distance at the bond length and the 12 A dissociation gate never fired."""
    pdb = tmp_path / "with_linker.pdb"
    _write_complex(pdb, binder_offset=74.0, linker_chain="L")
    with pytest.raises(RuntimeError, match="dissociated"):
        rsp._assert_product_bound(pdb, "B", rank=1, sequence="CREEEC")


def test_gate_multi_chain_target_renamed_binder_detected(tmp_path):
    """F1 regression: chain letters come from staging re-lettering, not YAML.
    A stale YAML-space binder name that happens to match a RECEPTOR copy must
    not measure receptor-receptor contacts (multi-chain target 4 A apart)."""
    pdb = tmp_path / "multi.pdb"
    _write_complex(pdb, binder_offset=74.0, target_chains=("A", "B"),
                   binder_chain="D")
    with pytest.raises(RuntimeError, match="dissociated"):
        rsp._assert_product_bound(pdb, "B", rank=4, sequence="CREEEC")


# -------------------------------------------- 3: keep-fallback seq reader

def test_pdb_peptide_sequence_reads_chain_b(tmp_path):
    pdb = tmp_path / "staged.pdb"
    _write_complex(pdb, binder_offset=2.0)
    assert rsp._pdb_peptide_sequence(pdb, "B") == "GASGASGAS"


def test_pdb_peptide_sequence_autodetects_relettered_binder(tmp_path):
    """Multi-chain staging re-letters the binder (here: 'D'); a hardcoded 'B'
    read the second RECEPTOR copy and the keep-fallback never matched."""
    pdb = tmp_path / "staged.pdb"
    _write_complex(pdb, binder_offset=2.0, target_chains=("A", "B"),
                   binder_chain="D")
    assert rsp._pdb_peptide_sequence(pdb, "D") == "GASGASGAS"
    # auto-detect: named chain is a receptor (44 res), binder found by size
    assert rsp._pdb_peptide_sequence(pdb, "B") == "GASGASGAS"

# ------------------------------- 4: D-target terminal-residue trim (1HL3)

def _uploaded_args(pdb_path: Path) -> tuple[dict, dict, dict]:
    payload = base64.b64encode(pdb_path.read_bytes()).decode("ascii")
    return (
        {"template_inputs": [{"content_base64": payload}]},
        {},  # base_yaml_data — unused when an upload is present
        {},  # options
    )


def test_d_target_trim_drops_incomplete_terminal_residues(tmp_path, monkeypatch):
    """Tail residue carrying a single N atom must not be staged: the model
    rebuilds it from the L-CCD token with L chirality and the D chirality
    gate then rejects every candidate (the 1HL3 THR332 incident)."""
    upload = tmp_path / "1hl3.pdb"
    _write_complex(upload, binder_offset=2.0)
    # append a broken terminal residue on chain A: name THR, only an N atom
    lines = upload.read_text().splitlines()
    serial = max(int(l[6:11]) for l in lines if l.startswith("ATOM")) + 1
    lines.append(
        f"ATOM  {serial:5d} N    THR A  45    "
        f"{0.0:8.3f}{22.8:8.3f}{0.0:8.3f}  1.00  0.00           N"
    )
    upload.write_text("\n".join(lines) + "\n")

    predict_args, base_yaml, options = _uploaded_args(upload)
    l_target, d_target = rsp._dpeptide_prepare_d_target(
        predict_args, base_yaml, options, "A", "protenix",
        tmp_path / "prep", 42)

    st = gemmi.read_structure(str(d_target))
    st.setup_entities()
    poly = [r for r in st[0]["A"] if r.het_flag != "H"]
    # chain A had 44 complete + 1 broken residue; broken tail must be trimmed
    assert len(poly) == 44
    assert {a.name for a in poly[-1]} >= {"N", "CA", "C"}
    # interior numbering is preserved (ordinal = position, head untouched)
    assert poly[0].seqid.num == 1 and poly[-1].seqid.num == 44


# --------------------------- 5: flip preserves bound geometry (sanity)

def test_flip_product_preserves_binding_geometry(tmp_path):
    from peplm.dpeptide.pipeline import flip_product
    src = tmp_path / "bound.pdb"
    _write_complex(src, binder_offset=2.6)
    out = tmp_path / "flipped.pdb"
    flip_product(src, out)
    assert _min_binder_distance(out) == pytest.approx(
        _min_binder_distance(src), abs=1e-3)
