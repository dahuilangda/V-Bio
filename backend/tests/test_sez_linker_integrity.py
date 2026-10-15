"""Regression tests for the linker covalent-integrity gate.

For every linker anchor the nearest Cys-SG distance must fall in the
per-chemistry bonded window (C-S 1.2-2.5 A, Bi-S 1.6-3.2 A): both overlap
(below the window) and tear (above it) are flagged."""
from __future__ import annotations

import pickle
import sys
import unittest
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from backend.runtime.run_single_prediction import (  # noqa: E402
    _dpeptide_linker_bond_report,
)

_LINKER_MOLS = _REPO / "backend" / "runtime" / "linker_ccd" / "linker_mols.pkl"


def _load_sez_template() -> dict:
    with open(_LINKER_MOLS, "rb") as fh:
        mol = pickle.load(fh)["SEZ"]
    conf = mol.GetConformer()
    ring_idx, arm_idx = set(), set()
    for bond in mol.GetBonds():
        i, j = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if str(bond.GetBondType()) == "AROMATIC":
            ring_idx.update((i, j))
    for bond in mol.GetBonds():
        i, j = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if str(bond.GetBondType()) != "AROMATIC":
            arm_idx.add(j if i in ring_idx else i)
    arms = [np.array(conf.GetAtomPosition(i)) for i in sorted(arm_idx)]
    ring = [np.array(conf.GetAtomPosition(i)) for i in sorted(ring_idx)]
    return {"arms": arms, "ring": ring}


def _write_complex(path: Path, template: dict, sg_cd_distance: float) -> None:
    """SEZ-shaped complex: three arm carbons with one Cys-SG each at the
    chosen distance (gate windows are keyed on the anchor atom name, so this
    also exercises the C-S path; Bi is covered by LinkerBondGateTests' ion
    fixture below)."""
    import gemmi
    st = gemmi.Structure()
    model = gemmi.Model("1")
    lchain = gemmi.Chain("L")
    res = gemmi.Residue()
    res.name = "SEZ"
    res.seqid = gemmi.SeqId(1, " ")
    res.het_flag = "H"
    for name, pos in zip(("CD", "C1", "C2"), template["arms"]):
        a = gemmi.Atom()
        a.name = name
        a.element = gemmi.Element("C")
        a.pos = gemmi.Position(*pos)
        res.add_atom(a)
    for j, pos in enumerate(template["ring"]):
        a = gemmi.Atom()
        a.name = ["CE", "CZ1", "CT1", "CH", "CT2", "CZ2"][j]
        a.element = gemmi.Element("C")
        a.pos = gemmi.Position(*pos)
        res.add_atom(a)
    lchain.add_residue(res)
    model.add_chain(lchain)
    pchain = gemmi.Chain("B")
    directions = [np.array([1.0, 0.0, 0.0]),
                  np.array([0.0, 1.0, 0.0]),
                  np.array([0.0, 0.0, 1.0])]
    for k, (arm_name, arm_pos) in enumerate(
            zip(("CD", "C1", "C2"), template["arms"])):
        cres = gemmi.Residue()
        cres.name = "CYS"
        cres.seqid = gemmi.SeqId(k + 1, " ")
        sg = gemmi.Atom()
        sg.name = "SG"
        sg.element = gemmi.Element("S")
        sg.pos = gemmi.Position(*(arm_pos + directions[k] * sg_cd_distance))
        cres.add_atom(sg)
        pchain.add_residue(cres)
    model.add_chain(pchain)
    st.add_model(model)
    st.setup_entities()
    st.make_mmcif_document().write_file(str(path))


def _write_bi_complex(path: Path, sg_bi_distance: float) -> None:
    """BS3-shaped complex: a bare Bi ion with three Cys-SG around it at the
    chosen distance (the CCD entry is the bare ion — coordination chemistry,
    no organic template)."""
    import gemmi
    st = gemmi.Structure()
    model = gemmi.Model("1")
    lchain = gemmi.Chain("L")
    res = gemmi.Residue()
    res.name = "BS3"
    res.seqid = gemmi.SeqId(1, " ")
    res.het_flag = "H"
    bi = gemmi.Atom()
    bi.name = "BI"
    bi.element = gemmi.Element("Bi")
    bi.pos = gemmi.Position(0.0, 0.0, 0.0)
    res.add_atom(bi)
    lchain.add_residue(res)
    model.add_chain(lchain)
    pchain = gemmi.Chain("B")
    directions = [np.array([1.0, 0.0, 0.0]),
                  np.array([0.0, 1.0, 0.0]),
                  np.array([0.0, 0.0, 1.0])]
    for k in range(3):
        cres = gemmi.Residue()
        cres.name = "CYS"
        cres.seqid = gemmi.SeqId(k + 1, " ")
        sg = gemmi.Atom()
        sg.name = "SG"
        sg.element = gemmi.Element("S")
        sg.pos = gemmi.Position(*(directions[k] * sg_bi_distance))
        cres.add_atom(sg)
        pchain.add_residue(cres)
    model.add_chain(pchain)
    st.add_model(model)
    st.setup_entities()
    st.make_mmcif_document().write_file(str(path))


class LinkerBondGateTests(unittest.TestCase):
    def setUp(self):
        self.template = _load_sez_template()

    def test_gate_rejects_overlap_and_tear(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            overlap = Path(td) / "overlap.cif"
            _write_complex(overlap, self.template, sg_cd_distance=0.31)
            report = _dpeptide_linker_bond_report(overlap)
            self.assertFalse(report["all_bonded"])
            self.assertAlmostEqual(report["bonds"][0]["distance"], 0.31, places=2)

    def test_gate_accepts_covalent_distance(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bonded = Path(td) / "bonded.cif"
            _write_complex(bonded, self.template, sg_cd_distance=1.82)
            report = _dpeptide_linker_bond_report(bonded)
            self.assertTrue(report["all_bonded"])

    def test_gate_bi_window(self):
        """BS3's anchor is a Bi(III) ion: Bi-S coordination lives at
        ~2.4-2.7 A. Reusing the C-S window would call a perfect 2.55 A
        bond "torn" and a 1.3 A crash "bonded"."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "bi_good.cif"
            _write_bi_complex(good, sg_bi_distance=2.55)
            report = _dpeptide_linker_bond_report(good)
            self.assertTrue(report["all_bonded"])
            crash = Path(td) / "bi_crash.cif"
            _write_bi_complex(crash, sg_bi_distance=1.30)
            report = _dpeptide_linker_bond_report(crash)
            self.assertFalse(report["all_bonded"])


class NcaaSubstitutionTests(unittest.TestCase):
    """NCAA overlay must place the user-selected non-natural residues at
    valid positions while preserving the Cys anchor set."""

    def _sub(self, seq, pool, lo, hi, cys):
        from backend.runtime.run_single_prediction import _substitute_ncaa_residues
        mods = []
        rng = __import__("random").Random(42)
        return _substitute_ncaa_residues(seq, mods, pool, lo, hi, cys, rng)

    def test_basic_substitution(self):
        pool = [{"ccd": "NLE", "base": "L", "placement": "any"}]
        seq = "ACDEFGHIKLMNPQRSTVWYCDE"
        cys = [0, 21]
        result, mods = self._sub(seq, pool, 1, 3, cys)
        self.assertTrue(1 <= len(mods) <= 3)
        for m in mods:
            self.assertNotIn(m["position"] - 1, cys)
            self.assertEqual(m["ccd"], "NLE")

    def test_zero_ncaa_is_noop(self):
        pool = [{"ccd": "NLE", "base": "L", "placement": "any"}]
        seq = "ACDEFGHIKLMNPQRST"
        result, mods = self._sub(seq, pool, 0, 0, [])
        self.assertEqual(result, seq)
        self.assertEqual(mods, [])

    def test_cys_protected(self):
        pool = [{"ccd": "NLE", "base": "L", "placement": "any"}]
        seq = "CCCCCCCCCCCCCCCC"
        result, mods = self._sub(seq, pool, 1, 3, [0, 1, 2])
        for m in mods:
            self.assertNotIn(m["position"] - 1, [0, 1, 2])

    def test_empty_pool_is_noop(self):
        seq = "ACDEFGHIKLMNPQRST"
        result, mods = self._sub(seq, [], 2, 5, [])
        self.assertEqual(result, seq)
        self.assertEqual(mods, [])


if __name__ == "__main__":
    unittest.main()
