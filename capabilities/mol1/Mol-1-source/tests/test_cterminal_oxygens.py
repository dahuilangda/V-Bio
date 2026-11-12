# Copyright 2026 Molence Bio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The C-terminal carboxyl oxygens are rebuilt symmetric about the C->CA axis."""

import unittest

import numpy as np
import torch
from biotite.structure import AtomArray

from mol.runner.inference import fix_cterminal_carboxyl_oxygens

# chain A: GLY 1 (N CA C O), GLY 2 (N CA C O OXT); chain B: a two-atom ligand.
ATOMS = [
    ("A", 1, "N"), ("A", 1, "CA"), ("A", 1, "C"), ("A", 1, "O"),
    ("A", 2, "N"), ("A", 2, "CA"), ("A", 2, "C"), ("A", 2, "O"), ("A", 2, "OXT"),
    ("B", 1, "C1"), ("B", 1, "O1"),
]
IDX = {(chain, res, name): i for i, (chain, res, name) in enumerate(ATOMS)}
CA, C, O, OXT = (IDX[("A", 2, name)] for name in ("CA", "C", "O", "OXT"))


def atom_array() -> AtomArray:
    array = AtomArray(len(ATOMS))
    array.chain_id = np.array([a[0] for a in ATOMS])
    array.res_id = np.array([a[1] for a in ATOMS])
    array.atom_name = np.array([a[2] for a in ATOMS])
    return array


def coordinates() -> torch.Tensor:
    """Two samples whose terminal C sits at the origin and CA on +x."""
    generator = torch.Generator().manual_seed(0)
    coords = torch.randn(2, len(ATOMS), 3, generator=generator) * 5
    coords[:, C] = torch.tensor([0.0, 0.0, 0.0])
    coords[:, CA] = torch.tensor([1.5, 0.0, 0.0])
    # Sample 0: O is in place and OXT has drifted away.
    coords[0, O] = torch.tensor([-0.6, 1.1, 0.0])
    coords[0, OXT] = torch.tensor([9.0, 9.0, 9.0])
    # Sample 1: the other way round, with the kept oxygen out of the xy plane.
    coords[1, O] = torch.tensor([-7.0, 8.0, 3.0])
    coords[1, OXT] = torch.tensor([-0.6, 0.0, 1.1])
    return coords


class CTerminalOxygenTest(unittest.TestCase):
    def test_nearest_oxygen_is_kept_and_the_other_mirrored(self):
        coords = coordinates()
        fixed = fix_cterminal_carboxyl_oxygens(coords, atom_array())
        expected_o = torch.tensor([[-0.6, 1.1, 0.0], [-0.6, 0.0, 1.1]])
        expected_oxt = torch.tensor([[-0.6, -1.1, 0.0], [-0.6, 0.0, -1.1]])
        torch.testing.assert_close(fixed[:, O], expected_o)
        torch.testing.assert_close(fixed[:, OXT], expected_oxt)

    def test_geometry_is_symmetric(self):
        fixed = fix_cterminal_carboxyl_oxygens(coordinates(), atom_array())
        c, ca, o, oxt = fixed[:, C], fixed[:, CA], fixed[:, O], fixed[:, OXT]
        torch.testing.assert_close((o - c).norm(dim=-1), (oxt - c).norm(dim=-1))
        axis = (ca - c) / (ca - c).norm(dim=-1, keepdim=True)
        cos_o = ((o - c) * axis).sum(-1) / (o - c).norm(dim=-1)
        cos_oxt = ((oxt - c) * axis).sum(-1) / (oxt - c).norm(dim=-1)
        torch.testing.assert_close(cos_o, cos_oxt)

    def test_only_the_terminal_oxygens_move(self):
        coords = coordinates()
        original = coords.clone()
        fixed = fix_cterminal_carboxyl_oxygens(coords, atom_array())
        torch.testing.assert_close(coords, original)  # input left alone
        others = [i for i in range(len(ATOMS)) if i not in (O, OXT)]
        torch.testing.assert_close(fixed[:, others], original[:, others])
        self.assertEqual(fixed.dtype, original.dtype)

    def test_chains_without_a_full_terminus_are_untouched(self):
        array = atom_array()
        array.atom_name[OXT] = "OX9"
        coords = coordinates()
        torch.testing.assert_close(fix_cterminal_carboxyl_oxygens(coords, array), coords)

    def test_mismatched_atom_count_is_returned_unchanged(self):
        coords = coordinates()[:, :-1]
        self.assertIs(fix_cterminal_carboxyl_oxygens(coords, atom_array()), coords)


if __name__ == "__main__":
    unittest.main()
