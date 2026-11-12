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

"""Where weights and reference data come from, and what a missing file does."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from mol.data.sources import (
    RELATIVE_PATH,
    RELEASE_URL,
    ReferenceDataMissing,
    checkpoint_key,
    checkpoint_url,
    missing_reference_data,
)


class TestDataSources(unittest.TestCase):
    def test_every_registered_model_has_a_weights_entry(self):
        from mol.config.model_type import model_configs

        for name in model_configs:
            with self.subTest(model=name):
                self.assertIn(checkpoint_key(name), RELATIVE_PATH)
                # The default local path is <load_checkpoint_dir>/<name>.pt,
                # so the released file has to carry the same name.
                self.assertEqual(
                    RELATIVE_PATH[checkpoint_key(name)].split("/")[-1], f"{name}.pt"
                )

    def test_default_model_is_registered(self):
        from mol.config.base import configs as base_configs
        from mol.config.inference import inference_configs
        from mol.config.model_type import model_configs

        self.assertIn(inference_configs["model_name"], model_configs)
        self.assertIn(base_configs["model_name"], model_configs)

    def test_default_checkpoint_is_the_released_file_name(self):
        from mol.config.inference import inference_configs
        from mol.runner import inference

        with tempfile.TemporaryDirectory() as root:
            weights = os.path.join(root, "mol-1-base.pt")
            open(weights, "wb").close()
            configs = SimpleNamespace(
                load_checkpoint_path="",
                load_checkpoint_dir=root,
                model_name=inference_configs["model_name"],
            )
            with mock.patch.object(inference, "download_from_url") as download:
                self.assertEqual(inference.resolve_checkpoint(configs), weights)
            download.assert_not_called()

    def test_missing_default_checkpoint_is_downloaded_from_the_release(self):
        from mol.runner import inference

        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "checkpoint")
            configs = SimpleNamespace(
                load_checkpoint_path="",
                load_checkpoint_dir=directory,
                model_name="mol-1-base",
            )
            with mock.patch.object(inference, "download_from_url") as download:
                path = inference.resolve_checkpoint(configs)
            self.assertEqual(path, os.path.join(directory, "mol-1-base.pt"))
            download.assert_called_once_with(
                f"{RELEASE_URL}/checkpoint/mol-1-base.pt", path, check_weight=True
            )

    def test_explicit_checkpoint_must_exist(self):
        from mol.runner import inference

        configs = SimpleNamespace(
            load_checkpoint_path="/nonexistent/weights.pt",
            load_checkpoint_dir="/nonexistent",
            model_name="mol-1-base",
        )
        with mock.patch.object(inference, "download_from_url") as download:
            with self.assertRaises(FileNotFoundError):
                inference.resolve_checkpoint(configs)
        download.assert_not_called()

    def test_missing_reference_data_stops_the_run_before_any_download(self):
        from ml_collections.config_dict import ConfigDict

        from mol.runner import inference

        with tempfile.TemporaryDirectory() as root:
            data = {
                name: os.path.join(root, RELATIVE_PATH[name])
                for name in (
                    "ccd_components_file",
                    "ccd_components_rdkit_mol_file",
                    "pdb_cluster_file",
                    "obsolete_release_data_csv",
                )
            }
            configs = ConfigDict(
                {
                    "data": data,
                    "use_template": False,
                    "load_checkpoint_path": "",
                    "load_checkpoint_dir": os.path.join(root, "checkpoint"),
                    "model_name": "mol-1-base",
                }
            )
            with mock.patch.object(inference, "download_from_url") as download:
                with self.assertRaises(ReferenceDataMissing) as caught:
                    inference.download_inference_cache(configs)
            download.assert_not_called()
            for path in data.values():
                self.assertIn(path, str(caught.exception))

    def test_released_weights_are_served_at_their_relative_path(self):
        from mol.config.model_type import model_configs

        for name in model_configs:
            with self.subTest(model=name):
                self.assertEqual(
                    checkpoint_url(name),
                    f"{RELEASE_URL}/{RELATIVE_PATH[checkpoint_key(name)]}",
                )
        with self.assertRaises(KeyError) as caught:
            checkpoint_url("not-a-model")
        self.assertIn("--load_checkpoint_path", str(caught.exception))

    def test_missing_reference_data_names_the_files_and_the_script(self):
        error = missing_reference_data(["/data/common/components.cif", "/data/x"])
        self.assertIsInstance(error, ReferenceDataMissing)
        self.assertIsInstance(error, FileNotFoundError)
        message = str(error)
        self.assertIn("/data/common/components.cif", message)
        self.assertIn("/data/x", message)
        self.assertIn("download_mol_data.sh", message)

    def test_relative_paths_are_relative(self):
        for name, path in RELATIVE_PATH.items():
            with self.subTest(name=name):
                self.assertFalse(path.startswith(("/", "http")))
                self.assertIn("/", path)


if __name__ == "__main__":
    unittest.main()
