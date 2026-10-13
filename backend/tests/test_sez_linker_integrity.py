"""Regression tests for the linker covalent-integrity gate.

The v15 L-TLE rank_01 shipped an SG-anchor pair at 0.31 A (overlap) with the
other two anchors torn (3.6/4.4 A) — the plain-route collect only rejected
fully-detached atoms. The report's bonded window (1.2-2.5 A) flags both
failure modes."""
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
    """Linker chain (ideal template) + one Cys per arm, SG at the chosen
    distance along a per-arm outward direction."""
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


if __name__ == "__main__":
    unittest.main()
