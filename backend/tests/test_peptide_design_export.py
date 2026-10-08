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


def _write_chain_pdb(path: Path, *, binder_offset: float) -> None:
    """Two chains: A = 6-residue target at origin, B = 3-residue binder.

    binder_offset shifts chain B along x. Residues get N/CA/C/CB (CB only on
    every second residue so the CB-optional path is exercised too)."""
    lines = []
    serial = 1

    def emit(chain: str, resi: int, resname: str, x: float, y: float, z: float,
             with_cb: bool) -> None:
        nonlocal serial
        for atom, (dx, dy, dz) in {
            "N": (1.2, 0.0, 0.0), "CA": (0.0, 0.0, 0.0),
            "C": (-1.2, 0.3, 0.0),
        }.items():
            # standard PDB columns: atom 13-16, resname 18-20, chain 22,
            # resseq 23-26, x from column 31 — the fixed-column readers under
            # test (line[21] chain etc.) require exactly this layout
            lines.append(
                f"ATOM  {serial:5d} {atom:<4} {resname:>3} {chain}{resi:4d}    "
                f"{x + dx:8.3f}{y + dy:8.3f}{z + dz:8.3f}  1.00  0.00           N"
            )
            serial += 1
        if with_cb:
            lines.append(
                f"ATOM  {serial:5d} CB   {resname:>3} {chain}{resi:4d}    "
                f"{x:8.3f}{y + 1.5:8.3f}{z:8.3f}  1.00  0.00           C"
            )
            serial += 1

    for i, resname in enumerate(THREE):
        emit("A", i + 1, resname, 0.0, i * 3.8, 0.0, with_cb=True)
    for i, resname in enumerate(["GLY", "ALA", "SER"]):
        emit("B", i + 1, resname, binder_offset, 0.0, 5.0, with_cb=(i != 0))
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
    _write_chain_pdb(pdb, binder_offset=74.0)
    assert _min_binder_distance(pdb) > 12.0
    with pytest.raises(RuntimeError, match="dissociated"):
        rsp._assert_product_bound(pdb, "B", rank=1, sequence="CREEEC")


def test_assert_product_bound_passes_real_docked_distances(tmp_path):
    for offset in (2.6, 5.0, 8.0):  # tight dock / typical / weak-but-bound
        pdb = tmp_path / f"bound_{offset}.pdb"
        _write_chain_pdb(pdb, binder_offset=offset)
        assert _min_binder_distance(pdb) <= 12.0
        got = rsp._assert_product_bound(pdb, "B", rank=2, sequence="CREEEC")
        assert got == pytest.approx(_min_binder_distance(pdb))


def test_assert_product_bound_unknown_binder_chain_fails(tmp_path):
    """A renamed binder chain must fail loudly, not silently pass."""
    pdb = tmp_path / "renamed.pdb"
    _write_chain_pdb(pdb, binder_offset=2.0)
    text = pdb.read_text().replace(" B", " X")
    pdb.write_text(text)
    with pytest.raises(RuntimeError, match="no binder chain"):
        rsp._assert_product_bound(pdb, "B", rank=3, sequence="CREEEC")


# -------------------------------------------- 3: keep-fallback seq reader

def test_pdb_peptide_sequence_reads_chain_b(tmp_path):
    pdb = tmp_path / "staged.pdb"
    _write_chain_pdb(pdb, binder_offset=2.0)
    seq = rsp._pdb_peptide_sequence(pdb, "B")
    assert seq == "GAS"
    assert rsp._pdb_peptide_sequence(pdb, "Z") == ""  # absent chain


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
    _write_chain_pdb(upload, binder_offset=2.0)
    # append a broken terminal residue on chain A: name THR, only an N atom
    lines = upload.read_text().splitlines()
    serial = max(int(l[6:11]) for l in lines if l.startswith("ATOM")) + 1
    lines.append(
        f"ATOM  {serial:5d} N    THR A   7    "
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
    # chain A had 6 complete + 1 broken residue; broken tail must be trimmed
    assert len(poly) == 6
    assert {a.name for a in poly[-1]} >= {"N", "CA", "C"}
    # interior numbering is preserved (ordinal = position, head untouched)
    assert poly[0].seqid.num == 1 and poly[-1].seqid.num == 6


# --------------------------- 5: flip preserves bound geometry (sanity)

def test_flip_product_preserves_binding_geometry(tmp_path):
    from peplm.dpeptide.pipeline import flip_product
    src = tmp_path / "bound.pdb"
    _write_chain_pdb(src, binder_offset=2.6)
    out = tmp_path / "flipped.pdb"
    flip_product(src, out)
    assert _min_binder_distance(out) == pytest.approx(
        _min_binder_distance(src), abs=1e-3)
