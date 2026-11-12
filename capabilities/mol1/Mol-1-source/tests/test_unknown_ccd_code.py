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

"""An input naming a CCD code the dictionary lacks fails with that code named."""

import os
import unittest

from mol.data.core import ccd
from mol.data.inference.json_parser import build_ligand, build_polymer

UNKNOWN = "NOTACODE"  # CCD codes are at most five characters


@unittest.skipUnless(
    os.path.isfile(ccd.COMPONENTS_FILE), "needs the CCD from the reference data"
)
class UnknownCcdCodeTest(unittest.TestCase):
    def assert_names_the_code(self, build, entity):
        with self.assertRaises(ValueError) as caught:
            build(entity)
        self.assertIn(repr(UNKNOWN), str(caught.exception))
        self.assertIn("SMILES", str(caught.exception))

    def test_ligand(self):
        self.assert_names_the_code(
            build_ligand, {"ligand": {"ligand": f"CCD_{UNKNOWN}", "count": 1}}
        )

    def test_one_of_several_ligand_components(self):
        self.assert_names_the_code(
            build_ligand, {"ligand": {"ligand": f"CCD_NAG_{UNKNOWN}", "count": 1}}
        )

    def test_protein_modification(self):
        self.assert_names_the_code(
            build_polymer,
            {
                "proteinChain": {
                    "sequence": "MKTAYIAK",
                    "count": 1,
                    "modifications": [{"ptmPosition": 3, "ptmType": f"CCD_{UNKNOWN}"}],
                }
            },
        )

    def test_nucleic_acid_modification(self):
        self.assert_names_the_code(
            build_polymer,
            {
                "dnaSequence": {
                    "sequence": "GATTACA",
                    "count": 1,
                    "modifications": [
                        {"basePosition": 2, "modificationType": f"CCD_{UNKNOWN}"}
                    ],
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
