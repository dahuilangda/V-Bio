"""Unit tests for boltz2score request validation (routes/affinity.py).

Covers the dock-pocket field validation (finiteness/magnitude, axis grouping, exactly-one
method) and the ligand_smiles_map contract — the guards added after the 2026-17 deep review.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.routes.affinity import (  # noqa: E402
    _parse_dock_pocket_fields,
    _parse_ligand_smiles_map,
)


class FakeForm(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class FakeFiles(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class DockPocketValidationTests(unittest.TestCase):
    def _parse(self, form):
        try:
            pocket, err, _missing = _parse_dock_pocket_fields(FakeForm(form), FakeFiles())
            return pocket, err, None
        except ValueError as exc:
            return None, str(exc), exc

    def test_non_finite_coordinates_rejected(self):
        for bad in ("nan", "inf", "-inf", "1e400"):
            _, err, _ = self._parse({"center_x": bad, "center_y": "0", "center_z": "0"})
            self.assertIsNotNone(err, bad)
            self.assertIn("finite", str(err).lower(), bad)

    def test_out_of_range_coordinates_rejected(self):
        _, err, _ = self._parse({"center_x": "99999", "center_y": "0", "center_z": "0"})
        self.assertIsNotNone(err)
        self.assertIn("within", str(err))

    def test_valid_center_passes(self):
        pocket, err, _ = self._parse({"center_x": "1.5", "center_y": "2", "center_z": "3"})
        self.assertIsNone(err)
        self.assertEqual(pocket["center_x"], 1.5)

    def test_partial_axes_rejected(self):
        _, err, _ = self._parse({"center_x": "1", "center_y": "2"})
        self.assertIsNotNone(err)
        self.assertIn("together", str(err))


class LigandSmilesMapTests(unittest.TestCase):
    def test_object_mapping_accepted(self):
        self.assertEqual(_parse_ligand_smiles_map('{"A": "CCO"}'), {"A": "CCO"})

    def test_non_object_rejected(self):
        for raw in ("[1,2]", '"x"', "3"):
            with self.assertRaises(ValueError, msg=raw):
                _parse_ligand_smiles_map(raw)

    def test_empty_returns_empty(self):
        self.assertEqual(_parse_ligand_smiles_map(""), {})
        self.assertEqual(_parse_ligand_smiles_map(None), {})


if __name__ == "__main__":
    unittest.main()
