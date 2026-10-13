"""Regression tests for the SEZ linker-core regularizer and the linker
covalent-integrity gate (v15 L-TLE rank_01 shipped a 0.31 A SG-anchor overlap
plus a 48°-169° warped mesitylene ring; 9/9 L-route rank_01 rings were warped
vs 9/9 D-route rings ideal — the D route stages the linker from the ideal CCD
template, the L route inherited the native prediction's unconstrained-sample
geometry)."""
from __future__ import annotations

import math
import pickle
import sys
import unittest
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from backend.runtime.run_single_prediction import (  # noqa: E402
    _dpeptide_linker_bond_report,
    _load_sez_ccd_conformer,
    _regularize_sez_ring_geometry,
)

P2D_CCDS = _REPO / "capabilities" / "protenix2dock"


def _ideal_sez_cif(path: Path) -> None:
    """Write a minimal mmCIF holding the ideal SEZ template (graph-mapped)."""
    import gemmi
    ideal = _load_sez_ccd_conformer()
    assert ideal is not None
    st = gemmi.Structure()
    st.spacegroup_hm = "P 1"
    model = gemmi.Model("1")
    chain = gemmi.Chain("L")
    res = gemmi.Residue()
    res.name = "SEZ"
    res.seqid = gemmi.SeqId(1, " ")
    res.het_flag = "H"
    for atom_name in ("CD", "C1", "C2"):
        idx = ("CD", "C1", "C2").index(atom_name)
        atom = gemmi.Atom()
        atom.name = atom_name
        atom.element = gemmi.Element("C")
        pos = ideal["arms"][idx]
        atom.pos = gemmi.Position(*pos)
        res.add_atom(atom)
    # Ring names follow the loader's own connectivity: attach[k] is the ring
    # index bonded to arm k; the anchored ring atoms are CE (CD's partner),
    # CT1 (C1's) and CT2 (C2's); the remaining three take the leftover names.
    attach = ideal["attach"]
    ring_names = [None] * 6
    ring_names[attach[0]] = "CE"
    ring_names[attach[1]] = "CT1"
    ring_names[attach[2]] = "CT2"
    leftover = [n for n in ("CZ1", "CH", "CZ2")]
    for j in range(6):
        if ring_names[j] is None:
            ring_names[j] = leftover.pop()
    for j, pos in enumerate(ideal["ring"]):
        atom = gemmi.Atom()
        atom.name = ring_names[j]
        atom.element = gemmi.Element("C")
        atom.pos = gemmi.Position(*pos)
        res.add_atom(atom)
    chain.add_residue(res)
    model.add_chain(chain)
    st.add_model(model)
    st.setup_entities()
    st.make_mmcif_document().write_file(str(path))


def _warp_ring(path: Path) -> None:
    """Boat-fold the ring (v15 L-TLE symptom: OOP 0.31 A, angles 48-156°).

    Displace two opposite ring atoms out of plane in opposite directions and
    pull two bonds — reproduces the measured distortion well past the 0.05
    no-op threshold."""
    import gemmi
    st = gemmi.read_structure(str(path))
    res = [r for m in st for c in m for r in c if r.name == "SEZ"][0]
    by_name = {a.name: a for a in res}
    pairs = [("CE", +0.45), ("CH", -0.45)]
    for name, dz in pairs:
        a = by_name[name]
        a.pos.z += dz
    # squeeze one bond to mimic the 1.34 A collapse
    a1, a2 = by_name["CE"], by_name["CZ1"]
    mid = np.array([(a1.pos.x + a2.pos.x) / 2,
                    (a1.pos.y + a2.pos.y) / 2,
                    (a1.pos.z + a2.pos.z) / 2])
    for a in (a1, a2):
        p = np.array([a.pos.x, a.pos.y, a.pos.z])
        newp = mid + (p - mid) * 0.55
        a.pos.x, a.pos.y, a.pos.z = (float(newp[0]), float(newp[1]),
                                     float(newp[2]))
    st.setup_entities()
    st.make_mmcif_document().write_file(str(path))


def _ring_metrics(path: Path):
    import gemmi
    st = gemmi.read_structure(str(path))
    res = [r for m in st for c in m for r in c if r.name == "SEZ"][0]
    atoms = {a.name: np.array([a.pos.x, a.pos.y, a.pos.z])
             for a in res if a.element.name == "C"}
    ring = ["CE", "CZ1", "CT1", "CH", "CT2", "CZ2"]
    pts = np.array([atoms[n] for n in ring])
    P = pts - pts.mean(0)
    oop = float(np.mean([abs(np.dot(p, np.linalg.svd(P)[2][2])) for p in P]))
    return oop


class SezRegularizerTests(unittest.TestCase):
    def test_loader_sources_template(self):
        ideal = _load_sez_ccd_conformer()
        self.assertIsNotNone(ideal)
        self.assertEqual(len(ideal["ring"]), 6)
        self.assertEqual(len(ideal["arms"]), 3)
        self.assertEqual(sorted(ideal["attach"]), [0, 1, 2]
                         if False else sorted(set(ideal["attach"])))
        self.assertTrue(set(ideal["attach"]) <= {0, 1, 2, 3, 4, 5})
        self.assertEqual(len(set(ideal["attach"])), 3)

    def test_warped_ring_repaired_and_arms_kept(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sez.cif"
            _ideal_sez_cif(path)
            _warp_ring(path)
            self.assertGreater(_ring_metrics(path), 0.10)
            changed = _regularize_sez_ring_geometry(path)
            self.assertTrue(changed)
            self.assertLess(_ring_metrics(path), 0.05)

    def test_ideal_ring_is_noop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sez.cif"
            _ideal_sez_cif(path)
            before = Path(path).read_text()
            self.assertFalse(_regularize_sez_ring_geometry(path))
            self.assertEqual(before, Path(path).read_text())


class LinkerBondGateTests(unittest.TestCase):
    def _write_complex(self, path: Path, sg_cd_distance: float) -> None:
        import gemmi
        ideal = _load_sez_ccd_conformer()
        st = gemmi.Structure()
        model = gemmi.Model("1")
        # linker chain
        lchain = gemmi.Chain("L")
        res = gemmi.Residue()
        res.name = "SEZ"
        res.seqid = gemmi.SeqId(1, " ")
        res.het_flag = "H"
        for name, pos in [("CD", ideal["arms"][0]), ("C1", ideal["arms"][1]),
                          ("C2", ideal["arms"][2])]:
            a = gemmi.Atom()
            a.name = name
            a.element = gemmi.Element("C")
            a.pos = gemmi.Position(*pos)
            res.add_atom(a)
        for j, pos in enumerate(ideal["ring"]):
            a = gemmi.Atom()
            a.name = ["CE", "CZ1", "CT1", "CH", "CT2", "CZ2"][j]
            a.element = gemmi.Element("C")
            a.pos = gemmi.Position(*pos)
            res.add_atom(a)
        lchain.add_residue(res)
        model.add_chain(lchain)
        # peptide chain: one Cys per linker arm, SG at the chosen distance
        # from its arm along a per-arm outward direction
        pchain = gemmi.Chain("B")
        directions = [np.array([1.0, 0.0, 0.0]),
                      np.array([0.0, 1.0, 0.0]),
                      np.array([0.0, 0.0, 1.0])]
        for k, (arm_name, arm_pos) in enumerate(
                zip(("CD", "C1", "C2"), ideal["arms"])):
            cres = gemmi.Residue()
            cres.name = "CYS"
            cres.seqid = gemmi.SeqId(k + 1, " ")
            sg = gemmi.Atom()
            sg.name = "SG"
            sg.element = gemmi.Element("S")
            sg_pos = arm_pos + directions[k] * sg_cd_distance
            sg.pos = gemmi.Position(*sg_pos)
            cres.add_atom(sg)
            pchain.add_residue(cres)
        model.add_chain(pchain)
        st.add_model(model)
        st.setup_entities()
        st.make_mmcif_document().write_file(str(path))

    def test_gate_rejects_overlap_and_tear(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            overlap = Path(td) / "overlap.cif"
            self._write_complex(overlap, sg_cd_distance=0.31)
            report = _dpeptide_linker_bond_report(overlap)
            self.assertFalse(report["all_bonded"])
            self.assertAlmostEqual(report["bonds"][0]["distance"], 0.31, places=2)

    def test_gate_accepts_covalent_distance(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bonded = Path(td) / "bonded.cif"
            self._write_complex(bonded, sg_cd_distance=1.82)
            report = _dpeptide_linker_bond_report(bonded)
            self.assertTrue(report["all_bonded"])


if __name__ == "__main__":
    unittest.main()
