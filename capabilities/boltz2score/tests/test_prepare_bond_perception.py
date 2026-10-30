"""Bounded ligand bond-perception tests.

Covers the regression where RDKit DetermineBonds spins for hours on highly
conjugated coordinate-only ligands (observed: 3 h on a 51-heavy-atom
RiTAC molecule), starving the whole scoring job until the task timeout.
"""

import subprocess
import time

import pytest
from rdkit import Chem

from core import prepare_inputs as pi


def _conjugated_coord_mol():
    """Coordinate-only mol as production builds it: 3D coords, all single bonds.

    PDB round-trip without CONECT records mirrors the staged CIF handoff
    (coordinates only, no bond orders).
    """
    from rdkit.Chem import AllChem

    template = Chem.MolFromSmiles("c1ccc2c(c1)ccc1ccccc12")
    AllChem.EmbedMolecule(template, randomSeed=42)
    pdb_block = Chem.MolToPDBBlock(template)
    mol = Chem.MolFromPDBBlock(pdb_block, removeHs=False, sanitize=False)
    mol = Chem.RemoveHs(mol, sanitize=False)
    for bond in mol.GetBonds():
        bond.SetBondType(Chem.rdchem.BondType.SINGLE)
        bond.SetIsAromatic(False)
    return mol


class TestAssignBondOrdersBounded:
    def test_normal_molecule_assigns_bonds(self):
        """A simple conjugated molecule gets non-single bonds within the budget."""
        mol = _conjugated_coord_mol()
        out = pi._assign_bond_orders(mol)
        assert out.GetNumBonds() == mol.GetNumBonds()
        non_single = sum(
            1 for b in out.GetBonds() if b.GetBondType() != Chem.rdchem.BondType.SINGLE
        )
        assert non_single > 0

    def test_bounded_helper_reports_timeout(self, monkeypatch):
        """A pathological perception is killed at the budget and reported incomplete."""
        big = Chem.MolFromSmiles("C" * 2 + "c1ccccc1" * 6)

        def slow_determine(mol, **kwargs):
            time.sleep(3600)

        monkeypatch.setattr(pi.rdDetermineBonds, "DetermineBonds", slow_determine)
        out, completed = pi._run_bounded_determine(big, 3.0)
        assert completed is False
        assert out.GetNumAtoms() == big.GetNumAtoms()

    def test_timeout_keeps_total_time_bounded(self, monkeypatch):
        """_assign_bond_orders returns within a small multiple of the budget."""
        mol = Chem.MolFromSmiles("c1ccccc1CCc1ccccc1")

        def slow_determine(mol_, **kwargs):
            time.sleep(3600)

        monkeypatch.setattr(pi.rdDetermineBonds, "DetermineBonds", slow_determine)
        monkeypatch.setattr(pi, "_BOND_PERCEPTION_TIMEOUT_SECONDS", 2.0)
        start = time.monotonic()
        out = pi._assign_bond_orders(mol)
        elapsed = time.monotonic() - start
        assert elapsed < 60.0  # first attempt + connectivity fallback, no full sweep
        assert out.GetNumAtoms() == mol.GetNumAtoms()

    def test_no_zombie_after_timeout(self):
        """Killed children are reaped, not left spinning or undead."""
        before = int(
            subprocess.run(
                ["sh", "-c", "ps -e -o stat= | grep -c '^Z' || true"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            or 0
        )
        pi._run_bounded_determine(Chem.MolFromSmiles("CCO"), 2.0)
        time.sleep(0.3)
        after = int(
            subprocess.run(
                ["sh", "-c", "ps -e -o stat= | grep -c '^Z' || true"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            or 0
        )
        assert after <= before + 1
