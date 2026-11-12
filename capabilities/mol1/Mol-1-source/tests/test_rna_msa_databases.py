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

import os
import tempfile
import unittest
from unittest.mock import patch

from mol.runner import rna_msa_search


class RnaDatabaseTest(unittest.TestCase):
    def test_missing_databases_are_reported_together_with_their_source(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.dict(os.environ, {"MOL_ROOT_DIR": root}):
            with self.assertRaises(FileNotFoundError) as caught:
                rna_msa_search.resolve_rna_databases(None, None, None)
        message = str(caught.exception)
        for _, filename, option in rna_msa_search.RNA_DATABASES:
            self.assertIn(f"{rna_msa_search.RNA_DATABASE_SOURCE}/{filename}.zst", message)
            self.assertIn(option, message)
        self.assertIn("--use_rna_msa false", message)

    def test_present_databases_are_used_as_they_are(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"MOL_ROOT_DIR": root}):
            os.makedirs(os.path.join(root, "search_database"))
            for _, filename, _ in rna_msa_search.RNA_DATABASES:
                open(os.path.join(root, "search_database", filename), "w").close()
            paths = rna_msa_search.resolve_rna_databases(None, None, None)
        self.assertEqual([os.path.basename(p) for p in paths],
                         [filename for _, filename, _ in rna_msa_search.RNA_DATABASES])

    def test_explicit_path_overrides_the_default(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"MOL_ROOT_DIR": root}):
            os.makedirs(os.path.join(root, "search_database"))
            for _, filename, _ in rna_msa_search.RNA_DATABASES[1:]:
                open(os.path.join(root, "search_database", filename), "w").close()
            custom = os.path.join(root, "my_nt.fasta")
            open(custom, "w").close()
            paths = rna_msa_search.resolve_rna_databases(custom, None, None)
        self.assertEqual(paths[0], custom)


if __name__ == "__main__":
    unittest.main()
