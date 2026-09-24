"""Unit tests for the live D-peptide mirror surface.

Fixture pair: 3LNJ native (L-MDM2 + D-PMI peptide, D-names renamed to L)
vs its exact mirror (D-MDM2 + L-peptide).
"""

from __future__ import annotations

import sys
from pathlib import Path

import gemmi
import numpy as np
import pytest

PEPTIDE_LM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PEPTIDE_LM_ROOT))

from peplm.dpeptide import (  # noqa: E402
    chirality_report,
    flip_product,
    mirror_structure,
)

FIXTURES = Path(__file__).parent / "fixtures"
NATIVE = FIXTURES / "3LNJ_native.pdb"
MIRROR = FIXTURES / "3LNJ_mirror.pdb"


def load(path):
    st = gemmi.read_structure(str(path))
    st.setup_entities()
    return st


class TestMirrorRoundTrip:
    def test_double_mirror_is_identity(self):
        st = load(NATIVE)
        mirror_structure(st)
        mirror_structure(st)
        orig = load(NATIVE)
        for ca, cb in zip(st[0][0], orig[0][0]):
            for a, b in zip(ca, cb):
                assert a.pos.x == pytest.approx(b.pos.x, abs=1e-6)
                assert a.pos.y == pytest.approx(b.pos.y, abs=1e-6)
                assert a.pos.z == pytest.approx(b.pos.z, abs=1e-6)

    def test_mirror_flips_chirality_both_chains(self):
        for chain, expect_native_l in (("A", True), ("B", False)):
            nat = chirality_report(load(NATIVE), chain)
            mir = chirality_report(load(MIRROR), chain)
            assert nat.n_scored > 0 and mir.n_scored > 0
            assert nat.is_l == expect_native_l
            assert mir.is_l != expect_native_l
            assert nat.mean_volume == pytest.approx(-mir.mean_volume, abs=1e-6)


class TestChiralityJudgement:
    def test_native_peptide_is_d(self):
        assert chirality_report(load(NATIVE), "B").is_l is False

    def test_mirror_peptide_is_l(self):
        assert chirality_report(load(MIRROR), "B").is_l is True


class TestProductFlip:
    def test_flip_restores_native_space(self, tmp_path):
        out = tmp_path / "product.pdb"
        flip_product(MIRROR, out)
        prod = load(out)
        native = load(NATIVE)
        pep_prod = np.array([[a.pos.x, a.pos.y, a.pos.z]
                             for r in prod[0]["B"] for a in r])
        pep_nat = np.array([[a.pos.x, a.pos.y, a.pos.z]
                            for r in native[0]["B"] for a in r])
        assert pep_prod.shape == pep_nat.shape
        assert float(np.abs(pep_prod - pep_nat).max()) < 1e-3
