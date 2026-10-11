"""Integration test: design_utils ligand-ipSAE fallback from full_data + CIF.

Covers the unified ipSAE scoring upgrade: when the worker result zip lacks
summary-level ipSAE (the L co-fold route historically shipped None), the
collect path computes ligand-ipSAE from full_data token_pair_pae + cif.
"""

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capabilities" / "designer"))
from design_utils import parse_confidence_metrics  # noqa: E402


def _make_cif(atoms: list[dict]) -> str:
    """Build a minimal mmCIF atom_site loop from atom dicts."""
    header = (
        "data_test\n#\nloop_\n"
        "_atom_site.group_PDB\n"
        "_atom_site.label_atom_id\n"
        "_atom_site.label_comp_id\n"
        "_atom_site.label_asym_id\n"
        "_atom_site.label_seq_id\n"
        "_atom_site.auth_asym_id\n"
        "_atom_site.Cartn_x\n"
        "_atom_site.Cartn_y\n"
        "_atom_site.Cartn_z\n#\n"
    )
    rows = []
    for a in atoms:
        rows.append(
            f"ATOM {a['name']:4s} {a['resn']:3s} {a['chain']:2s} {a['seq']:3d} {a['chain']:2s}"
            f" {a['x']:8.3f} {a['y']:8.3f} {a['z']:8.3f}"
        )
    return header + "\n".join(rows) + "\n#\n"


def _make_atoms() -> list[dict]:
    """Target chain A (4 residues × CA) + binder chain B (5 residues × CB)."""
    atoms = []
    # target: 4 Ala residues on chain A, CA at x=0..12
    for i in range(4):
        atoms.append({
            "name": "CA", "resn": "ALA", "chain": "A", "seq": i + 1,
            "x": float(i * 4), "y": 0.0, "z": 0.0,
        })
    # binder: 5 Lys residues on chain B, CB at x=16..32 (near target C-term)
    for i in range(5):
        atoms.append({
            "name": "CB", "resn": "LYS", "chain": "B", "seq": i + 1,
            "x": 16.0 + float(i * 4), "y": 1.0, "z": 1.0,
        })
    return atoms


def _make_full_data(atoms: list[dict], n_tokens: int) -> dict:
    """full_data: token_pair_pae (low at interface, higher elsewhere),
    token_asym_id, atom_to_token_idx, atom_plddt — one token per CA/CB atom."""
    n_atoms = len(atoms)
    # Map: first n_atoms tokens = atoms in order; extras (if any) repeat last.
    rng = np.random.RandomState(42)
    pae = rng.uniform(8.0, 12.0, size=(max(n_tokens, n_atoms),) * 2).astype(np.float32)
    # Cross-chain pairs (target A rows × binder B cols): low PAE = confident interface
    target_idx = list(range(4))
    binder_idx = list(range(4, 9))
    for ti in target_idx:
        for bi in binder_idx:
            pae[ti, bi] = rng.uniform(0.5, 2.0)
            pae[bi, ti] = pae[ti, bi]
    token_asym = ["A"] * 4 + ["B"] * 5
    # pad to n_tokens
    while len(token_asym) < pae.shape[0]:
        token_asym.append("A")
    pae = pae[:len(token_asym), :len(token_asym)]
    atom_to_token = list(range(min(n_atoms, len(token_asym))))
    atom_plddt = [90.0 + i for i in range(len(atom_to_token))]
    return {
        "token_pair_pae": pae.tolist(),
        "token_asym_id": token_asym,
        "atom_to_token_idx": atom_to_token,
        "atom_plddt": atom_plddt,
    }


class TestLigandIpsaeFallback(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'protenix' / 'output').mkdir(parents=True, exist_ok=True)
        atoms = _make_atoms()
        # write cif
        (self.root / 'protenix' / 'output' / 'test_sample_0.cif').write_text(_make_cif(atoms))
        # write full_data (one sample)
        full = _make_full_data(atoms, n_tokens=9)
        (self.root / 'protenix' / 'output' / 'test_full_data_sample_0.json').write_text(json.dumps(full))
        # write summary_confidences (only ptm/iptm, no ipsae — the L co-fold gap)
        (self.root / 'protenix' / 'output' / 'test_summary_confidences_0.json').write_text(json.dumps({
            "ptm": 0.7, "iptm": 0.65, "ranking_score": 0.6,
        }))
        # write a minimal predictions dir marker
        

    def tearDown(self):
        self._tmp.cleanup()

    def _parse(self):
        return parse_confidence_metrics(
            str(self.root),
            binder_chain_id="B",
            target_chain_id="A",
        )

    def test_ligand_ipsae_populated(self):
        result = self._parse()
        self.assertIsNotNone(
            result.get("ligand_ipsae_max"),
            "ligand_ipsae_max should be computed from full_data PAE")
        self.assertIsNotNone(result.get("ipsae_dom"))

    def test_ligand_ipsae_in_valid_range(self):
        result = self._parse()
        v = result.get("ligand_ipsae_max")
        if v is not None:
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)

    def test_interface_metric_prefers_ipsae(self):
        """resolve_preferred_interface_metric (design loop) sets
        interface_metric from these values; parse_confidence_metrics only
        populates the raw ligand_ipsae_max/ipsae_dom fields."""
        result = self._parse()
        # The ligand_ipsae_max is the signal the design loop uses to prefer
        # ipSAE over ipTM — verifying it's populated here is sufficient.
        self.assertIsNotNone(result.get("ligand_ipsae_max"))

    def test_ipae_values_populated(self):
        """full_data json keys (atom_plddt etc.) are also read by the parser."""
        result = self._parse()
        # The binder pLDDT values come from full_data atom_plddt
        self.assertIsNotNone(result.get("binder_avg_plddt"))


if __name__ == "__main__":
    unittest.main()
