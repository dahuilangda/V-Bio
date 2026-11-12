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

"""Kalign is the one on PATH; templates without it stop before the model loads."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from mol.data.tools.kalign import Kalign, find_kalign
from mol.runner import batch_inference


def fake_kalign(directory: str) -> str:
    path = os.path.join(directory, "kalign")
    Path(path).write_text("#!/bin/sh\nexit 0\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


class KalignLookupTest(unittest.TestCase):
    def test_the_kalign_on_path_is_used(self):
        with tempfile.TemporaryDirectory() as d:
            path = fake_kalign(d)
            with patch.dict(os.environ, {"PATH": d}):
                self.assertEqual(find_kalign(), path)
                self.assertEqual(Kalign().binary_path, path)

    def test_missing_kalign_says_how_to_install_it(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"PATH": d}):
            with self.assertRaises(RuntimeError) as caught:
                find_kalign()
        message = str(caught.exception)
        self.assertIn("apt-get install kalign", message)
        self.assertIn("kalign3=3.3.5", message)

    def test_templates_without_kalign_stop_before_the_model_is_built(self):
        with tempfile.TemporaryDirectory() as d, \
             patch.dict(os.environ, {"PATH": d}), \
             patch.object(batch_inference, "InferenceRunner") as build, \
             patch.object(batch_inference, "download_inference_cache"):
            with self.assertRaises(RuntimeError):
                batch_inference.get_default_runner(use_template=True)
        build.assert_not_called()

    def test_mol_pred_has_no_kalign_path_option(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.json").write_text("[]")
            with patch.object(batch_inference, "inference_jsons") as run:
                result = CliRunner().invoke(
                    batch_inference.mol_cli,
                    ["pred", "-i", str(Path(d) / "a.json"), "-o", d,
                     "--kalign_binary_path", "/usr/bin/kalign"],
                )
        self.assertEqual(result.exit_code, 2, result.output)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
