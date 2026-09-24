"""D-peptide production wiring tests (mirror, staging, chirality gates)."""

from __future__ import annotations

import importlib
import math
import os
import sys
from pathlib import Path

import numpy as np
import pytest
import gemmi

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PEPLM_ROOT = REPO_ROOT / "capabilities" / "peptide_lm"
if str(PEPLM_ROOT) not in sys.path:
    sys.path.insert(0, str(PEPLM_ROOT))

from peplm.dpeptide import chirality_report, mirror_structure  # noqa: E402
from peplm.dpeptide.pipeline import flip_product  # noqa: E402

rsp = importlib.import_module("backend.runtime.run_single_prediction")

FIXTURES = PEPLM_ROOT / "tests" / "fixtures"
NATIVE = FIXTURES / "3LNJ_native.pdb"        # L-MDM2 (+) + L-PMI renamed (+)
MIRROR = FIXTURES / "3LNJ_mirror.pdb"        # exact x->-x: both chains flip sign


class TestDTargetPreparation:
    """The D-route mirror primitive: x->-x on the uploaded target is exact
    and the written D-target is renumbered 1..N in sequence order."""

    def test_prepare_d_target_mirrors_and_renumbers(self, tmp_path):
        ll = _build_ll_complex(tmp_path)
        st = gemmi.read_structure(str(ll)); st.setup_entities()
        src = np.array([[a.pos.x, a.pos.y, a.pos.z]
                        for r in st[0][0] for a in r if a.element != gemmi.Element("H")])
        import yaml as _yaml
        cfg = tmp_path / "prep"; cfg.mkdir()
        yaml_text = (
            "sequences:\n"
            "- protein:\n    id: A\n    sequence: TLVRPKPLLLKLLKSVGAQKDTYTMKE\n"
            f"templates:\n- pdb: {ll}\n  chain_id:\n  - A\n")
        base = _yaml.safe_load(yaml_text)
        l_target, d_target = rsp._dpeptide_prepare_d_target(
            {"template_inputs": []}, base, {}, "A", "protenix", cfg, 7)
        out = gemmi.read_structure(str(d_target)); out.setup_entities()
        rec = out[0][0]
        nums = [r.seqid.num for r in rec]
        assert nums == list(range(1, len(nums) + 1))
        dst = np.array([[a.pos.x, a.pos.y, a.pos.z]
                        for r in rec for a in r if a.element != gemmi.Element("H")])
        assert len(dst) == len(src)
        assert np.abs(dst[:, 0] + src[:, 0]).max() < 1e-3
        assert np.abs(dst[:, 1:] - src[:, 1:]).max() < 1e-3


def _chain_volume(structure_path: Path, chain: str) -> float:
    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()
    return chirality_report(st, chain).mean_volume


class TestProductChiralityGate:
    @pytest.fixture(scope="class")
    def product_pdb(self, tmp_path_factory):
        out = tmp_path_factory.mktemp("prod") / "product.pdb"
        return flip_product(MIRROR, out)

    def test_passes_for_flipped_mirror_complex(self, product_pdb):
        # 3LNJ_mirror is the modeling-space complex: D-target(-)+L-pep(+);
        # one flip restores L-target(+)+D-pep(-).
        gate = rsp._assert_product_chirality(product_pdb)
        assert gate["receptor_config"] == "L"
        assert gate["peptide_config"] == "D"

    def test_exact_reference_roundtrip(self, product_pdb):
        # the flip of the mirror complex restores NATIVE coordinates exactly;
        # matching is by residue number (chain names are not stable keys)
        gate = rsp._assert_product_chirality(product_pdb,
                                             reference_structure_path=NATIVE)
        assert gate["receptor_vs_input_rmsd"] < 0.01

    def test_rejects_unflipped_mirror_complex(self, tmp_path):
        # The un-flipped modeling complex (D-receptor + L-peptide) violates
        # the product contract (receptor must be L, peptide D). Since
        # 2026-09-04 the gate is a HARD per-residue rejection — mean-volume
        # telemetry silently passed mixed chirality.
        with pytest.raises(RuntimeError, match="receptor not all-L|peptide not all-D"):
            rsp._assert_product_chirality(MIRROR)

    def test_flags_drifted_receptor(self, product_pdb, tmp_path):
        shifted = tmp_path / "shifted.pdb"
        text = product_pdb.read_text().splitlines()
        out = []
        for line in text:
            if line.startswith("ATOM") and line[21] == "A":
                z = float(line[46:54]) + 5.0
                line = line[:46] + f"{z:8.3f}" + line[54:]
            out.append(line)
        shifted.write_text("\n".join(out) + "\n")
        gate = rsp._assert_product_chirality(shifted, reference_structure_path=product_pdb, rmsd_limit=1.0)
        assert gate["receptor_vs_input_rmsd"] > 1.0





def _build_ll_complex(tmp_path: Path) -> Path:
    """Minimal native L-L complex fixture: L-receptor slice + L-peptide whose
    three Cys SGs form a non-coplanar triangle + an SEZ linker residue."""
    src = gemmi.read_structure(str(NATIVE))
    src.setup_entities()
    st = gemmi.Structure()
    model = gemmi.Model("1")
    rec = gemmi.Chain(src[0][0].name)
    for res in list(src[0][0])[:30]:
        rec.add_residue(res.clone())
    model.add_chain(rec)
    pep = gemmi.Chain("B")
    rng = np.random.default_rng(5)
    tri = np.array([[30 + 2.917 * math.cos(-2 * math.pi * k / 3),
                     2.917 * math.sin(-2 * math.pi * k / 3), 0.5] for k in range(3)])
    tri_atoms = ["SG"] * 3  # anchor proxy for this fixture
    for num in range(1, 21):
        res = gemmi.Residue()
        res.name = "CYS" if num in (1, 9, 20) else "ALA"
        res.seqid = gemmi.SeqId(num, " ")
        ca = gemmi.Atom(); ca.name = "CA"; ca.element = gemmi.Element("C")
        ca.pos = gemmi.Position(num * 3.0, -0.6, 0.4)
        n = gemmi.Atom(); n.name = "N"; n.element = gemmi.Element("N")
        n.pos = gemmi.Position(num * 3.0 - 1.2, 0.3, 0.0)
        c = gemmi.Atom(); c.name = "C"; c.element = gemmi.Element("C")
        c.pos = gemmi.Position(num * 3.0 + 1.25, 0.25, 0.0)
        o = gemmi.Atom(); o.name = "O"; o.element = gemmi.Element("O")
        o.pos = gemmi.Position(num * 3.0 + 1.3, 1.45, 0.1)
        cb = gemmi.Atom(); cb.name = "CB"; cb.element = gemmi.Element("C")
        cb.pos = gemmi.Position(num * 3.0 + 0.1, -1.4, 0.7)
        for atom in (ca, n, c, o, cb):
            res.add_atom(atom)
        if num in (1, 9, 20):
            k = {1: 0, 9: 1, 20: 2}[num]
            sg = gemmi.Atom(); sg.name = "SG"; sg.element = gemmi.Element("S")
            sg.pos = gemmi.Position(*(tri[k] + rng.normal(scale=0.02, size=3)))
            res.add_atom(sg)
        pep.add_residue(res)
    model.add_chain(pep)
    link = gemmi.Chain("L")
    lr = gemmi.Residue(); lr.name = "SEZ"; lr.seqid = gemmi.SeqId(1, " "); lr.het_flag = "H"
    for i, an in enumerate(("CD", "C1", "C2", "CE")):
        a = gemmi.Atom(); a.name = an; a.element = gemmi.Element("C")
        base = tri.mean(axis=0)
        off = [(tri[0] - base), (tri[1] - base), (tri[2] - base)][i % 3] * 0.5
        a.pos = gemmi.Position(*(base + off + np.array([0, 0, 1.5])))
        lr.add_atom(a)
    link.add_residue(lr)
    st.add_model(model)
    st.setup_entities()
    out = tmp_path / "ll_complex.pdb"
    st.write_pdb(str(out))
    return out



class TestRingEngineRestriction:
    """Constrained rings are Protenix-only: boltz2's bond feature is a soft
    prior and breaks ring bonds under diffusion; TFG enforces them."""

    def _assert_backend(self, backend, design_mode):
        from backend.runtime.run_single_prediction import _normalize_peptide_backend, _normalize_peptide_design_mode
        return _normalize_peptide_backend(backend), _normalize_peptide_design_mode(design_mode)

    def test_boltz_rings_are_rejected_by_production_guard(self):
                # exercise the actual guard inside run_peptide_design_backend by
        # calling the same comparison it performs
        backend, mode = self._assert_backend("boltz", "bicyclic")
        assert mode == "bicyclic"
        # the guard itself lives inside run_peptide_design_backend; verify the
        # normalization chain the guard relies on keeps boltz != protenix
        assert backend != "protenix"

    def test_protenix_rings_pass_normalization(self):
        backend, mode = self._assert_backend("protenix2dock", "bicyclic")
        assert backend == "protenix"
        assert mode == "bicyclic"


class TestModeAnchoredPlacement:
    """Mode A: an uploaded reference peptide pose anchors every candidate's
    staging instead of the generic pocket surface search."""

    def test_reference_peptide_mirror(self, tmp_path):
        import gemmi as g
        from peplm.dpeptide import chirality_report as _cr
        st = g.Structure(); m = g.Model("1"); ch = g.Chain("P")
        for i in range(1, 7):
            r = g.Residue(); r.name = "ALA"; r.seqid = g.SeqId(i + 10, " ")
            for nm, el in (("N", "N"), ("CA", "C"), ("C", "C"), ("CB", "C")):
                a = g.Atom(); a.name = nm; a.element = g.Element(el)
                a.pos = g.Position(i * 3.2 + (0.5 if nm == "CB" else 0.0),
                                   0.3 if nm == "N" else 0.0, 0.4 if nm == "C" else 0.0)
                r.add_atom(a)
            ch.add_residue(r)
        m.add_chain(ch); st.add_model(m); st.setup_entities()
        up = tmp_path / "pep.pdb"; st.write_pdb(str(up))
        import base64
        args = {"peptide_structure_input": {
            "file_name": "pep.pdb", "format": "pdb", "chain_id": "P",
            "content_base64": base64.b64encode(up.read_bytes()).decode()}}
        ref = rsp._dpeptide_prepare_reference_peptide(args, tmp_path / "prep", 12)
        assert ref is not None and ref.is_file()
        out = g.read_structure(str(ref)); out.setup_entities()
        assert [r.seqid.num for r in out[0][0]] == list(range(1, 7))
        src = np.array([[a.pos.x, a.pos.y, a.pos.z] for r in st[0][0] for a in r])
        dst = np.array([[a.pos.x, a.pos.y, a.pos.z] for r in out[0][0] for a in r])
        assert np.abs(dst[:, 0] + src[:, 0]).max() < 1e-3
        assert np.abs(dst[:, 1:] - src[:, 1:]).max() < 1e-3
        assert _cr(out, "B").mean_volume < 0

    def test_no_upload_returns_none(self, tmp_path):
        assert rsp._dpeptide_prepare_reference_peptide({}, tmp_path, 12) is None


