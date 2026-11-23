from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import yaml
import pytest

from backend.runtime import rfd4_adapter as rb

RUNTIME_DIR = Path(rb.__file__).resolve().parent
SEED = "AARFKVWW"

# canonical mmcif atom_site columns; gemmi needs these to build a Structure
CIF_TAGS = ["group_PDB", "id", "type_symbol", "label_atom_id", "label_alt_id",
            "label_comp_id", "label_asym_id", "label_entity_id", "label_seq_id",
            "pdbx_PDB_ins_code", "Cartn_x", "Cartn_y", "Cartn_z", "occupancy",
            "B_iso_or_equiv", "auth_seq_id", "auth_comp_id", "auth_asym_id",
            "auth_atom_id", "pdbx_PDB_model_num"]


def cif_atom(aid, sym, atom, comp, asym, seq, x, y, z):
    return (f"ATOM {aid} {sym} {atom} . {comp} {asym} 1 {seq} ? "
            f"{x:.3f} {y:.3f} {z:.3f} 1.00 20.00 {seq} {comp} {asym} {atom} 1")


def write_cif(path, rows):
    lines = ["data_x", "#", "loop_"] + [f"_atom_site.{t}" for t in CIF_TAGS] + rows
    path.write_text("\n".join(lines) + "\n")


_SKELETON_CACHE = {}


def build(seed=SEED, ncaa="NLE", length=15):
    """Memoized skeleton build: closures cost ~100 s each."""
    key = (seed, ncaa, length)
    if key not in _SKELETON_CACHE:
        anchors, sez = rb.load_anchor_skeleton(RUNTIME_DIR, "ZBR")
        _SKELETON_CACHE[key] = rb.build_bicyclic_skeleton(seed, ncaa, length, anchors, sez)
    return _SKELETON_CACHE[key]


def _bond_violations(residues, limit=4.0):
    pairs = (("N", "CA"), ("CA", "C"), ("CA", "CB"), ("CB", "SG"), ("C", "O"))
    out = []
    for comp, atoms in residues:
        for a, b in pairs:
            if a in atoms and b in atoms:
                d = float(np.linalg.norm(atoms[a] - atoms[b]))
                if d > limit:
                    out.append((comp, a, b, round(d, 2)))
    return out


class TestChirality:
    def test_bridge_residues_are_l_form(self):
        # CB reference atom must be the same-residue C: alternating/degenerate
        # triple products would mean D-amino staging geometry
        import numpy as np
        anchors, sez = rb.load_anchor_skeleton(RUNTIME_DIR, "ZBR")
        residues, _sez, _letters, bonds = build()
        signs = []
        for comp, atoms in residues:
            if comp in ("CYS",) and "SG" in atoms:
                continue
            if not {"N", "CA", "C", "CB"} <= set(atoms):
                continue
            signs.append(round(float(np.dot(
                np.cross(atoms["N"] - atoms["CA"], atoms["C"] - atoms["CA"]),
                atoms["CB"] - atoms["CA"])), 2))
        assert signs and all(s > 0.5 for s in signs), signs

    def test_too_wide_span_rejected_fast(self):
        # a span beyond per-residue reach must fail before the optimizer runs
        anchors, sez = rb.load_anchor_skeleton(RUNTIME_DIR, "ZBR")
        stretched = {1: dict(anchors[1]), 2: dict(anchors[2]), 3: dict(anchors[3])}
        stretched[1]["C"] = anchors[1]["C"] + np.array([40.0, 0.0, 0.0])
        with pytest.raises(ValueError, match="unreachable"):
            rb.build_bicyclic_skeleton(SEED, "NLE", 15, stretched, sez)


class TestSkeleton:
    def test_load_anchor_skeleton(self):
        anchors, sez = rb.load_anchor_skeleton(RUNTIME_DIR, "ZBR")
        assert sorted(anchors) == [1, 2, 3]
        for atoms in anchors.values():
            assert {"N", "CA", "C", "O", "CB", "SG"} <= set(atoms)
        assert len(sez) >= 3

    def test_closure_and_layout(self):
        residues, sez_out, letters, bonds = build()
        assert len(residues) == 15
        assert all(abs(b - rb.B_C_N) <= rb.CLOSURE_TOL for b in bonds)
        assert letters.startswith("C" + SEED + "CL")
        assert letters.endswith("C")
        assert letters.count("C") == 3
        assert _bond_violations(residues) == []
        ncaa_atoms = next(atoms for comp, atoms in residues if comp == "NLE")
        assert set(ncaa_atoms) == {"N", "CA", "C", "O"} | set(rb.NCAA_ATOMS["NLE"])

    def test_no_ncaa(self):
        residues, _sez, letters, _bonds = build(ncaa=None)
        assert letters == "C" + SEED + "CXXXXC"

    def test_aib_sidechain_atoms(self):
        residues, _sez, _letters, _bonds = build(ncaa="AIB")
        atoms = next(a for comp, a in residues if comp == "AIB")
        assert set(atoms) == {"N", "CA", "C", "O", "CB1", "CB2"}

    def test_too_short_rejected(self):
        # closure ring = 2 residues, below the hard floor
        with pytest.raises(ValueError, match="length too short"):
            build(length=13)

    def test_closure_gate_rejected(self, monkeypatch):
        # dynamic gate: a bond stalled far from ideal must raise
        import numpy as np

        def stalled(*_args, **_kwargs):
            return [{"N": np.zeros(3), "CA": np.zeros(3), "C": np.zeros(3),
                     "O": np.zeros(3), "CB": np.zeros(3)}], 3.56

        monkeypatch.setattr(rb, "_closure_segment", stalled)
        _SKELETON_CACHE.clear()
        try:
            with pytest.raises(ValueError, match="closure failed"):
                build()
        finally:
            _SKELETON_CACHE.clear()


class TestPocketCentroid:
    def _write_target(self, tmp_path):
        rows = []
        aid = 0
        for chain, seqs in (("A", (10, 11)), ("D", (5,))):
            for seq in seqs:
                x = 0.0 if chain == "A" else 8.0
                aid += 1
                rows.append(cif_atom(aid, "N", "N", "GLY", chain, seq, x, 0.0, 0.0))
                aid += 1
                rows.append(cif_atom(aid, "C", "CA", "GLY", chain, seq, x, 1.2, 0.0))
        out = tmp_path / "target.cif"
        write_cif(out, rows)
        return out

    def test_centroid_matches(self, tmp_path):
        cif = self._write_target(tmp_path)
        c = rb.pocket_centroid(cif, [("A", 10), ("D", 5)])
        assert np.allclose(c, [4.0, 1.2, 0.0])

    def test_missing_residue_rejected(self, tmp_path):
        cif = self._write_target(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            rb.pocket_centroid(cif, [("A", 10), ("D", 999)])


class TestCombinedAndSpec:
    def test_combined_and_spec(self, tmp_path):
        residues, sez, _letters, _bonds = build()
        target = tmp_path / "t.cif"
        rows = [cif_atom(1, "N", "N", "GLY", "B", 1, 0, 0, 0)]  # collides with peptide B
        rows += [cif_atom(2 + i, "N", "N", "GLY", "D", seq, 1, 1, 1)
                 for i, seq in enumerate(range(1, 82), start=0)]
        write_cif(target, rows)

        combined = tmp_path / "combined.cif"
        chains = rb.write_combined_cif(combined, "stem", residues, sez, "SEZ", target)
        assert ("A", 1) in chains and ("D", 81) in chains
        cent = rb.chain_centroids(combined)
        assert set(cent) == {"A", "B", "C", "D"}

        spec = yaml.safe_load(rb.build_spec("stem", len(SEED), True, 15, chains, cent))["stem"]
        assert spec["contig"] == "B1-15, /0, C1, /0, A1-1, /0, D1-81"
        cond = spec["conditions"]
        # YAML parses the bare key `true` as boolean
        cseq = [c["select"] for c in cond["C_SEQ"][True]]
        assert "sel('B/*/1')" in cseq and "sel('B/*/[2-9]')" in cseq
        assert "sel('B/*/10')" in cseq and "sel('B/*/11')" in cseq
        assert "sel('B/*/15')" in cseq
        assert not any(s in cseq for s in ("sel('B/*/12')", "sel('B/*/13')", "sel('B/*/14')"))
        assert any("B/CYS/*/SG" in c["select"] for c in cond["C_CRD"][True])
        # target backbone is rigid-body only: absolute coords in C_CRD would
        # conflict with the coordinate group ("modify C_CRD first")
        assert not any("A/*/*/[N,CA,C,O]" in c["select"] for c in cond["C_CRD"][True])
        assert any("A/*/*/[N,CA,C,O]" in c["select"] for c in cond["C_CRDGRP"][1])

    def test_spec_without_ncaa(self):
        build(ncaa=None)
        text = rb.build_spec("s", len(SEED), False, 15, [("A", 10)], {})
        assert "sel('B/*/11')" not in text


class TestBinderExtraction:
    def test_binder_and_yaml(self):
        rows = []
        aid = 0
        for i, comp in enumerate(["CYS", "ALA", "NLE", "CYS", "GLY", "CYS"], start=1):
            for atom in ("N", "CA", "C", "O", "CB"):
                aid += 1
                rows.append(cif_atom(aid, "C", atom, comp, "B", i, 0, 0, 0))
        binder, mods, cys = rb.binder_from_cif_text(
            "\n".join(["data_g", "#", "loop_"] +
                      [f"_atom_site.{t}" for t in CIF_TAGS] + rows) + "\n")
        assert binder == "CALCGC"
        assert mods == [{"position": 3, "ccd": "NLE", "baseResidue": "L"}]
        assert cys == [1, 4, 6]



def test_combined_template_input(tmp_path):
    cif = tmp_path / "c.cif"
    cif.write_text("data_x\n")
    entry = rb.combined_template_input(cif)
    assert entry["file_name"] == "c.cif"
    assert entry["format"] == "cif"
    assert json.dumps(entry)
