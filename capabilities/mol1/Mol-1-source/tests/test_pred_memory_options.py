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

"""mol pred takes the inference memory controls under the runner's names."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import torch
from click.testing import CliRunner

from mol.runner import batch_inference, inference


class PredMemoryOptionsTest(unittest.TestCase):
    def invoke_pred(self, *options):
        """Run mol pred with the runner stubbed out; return the result and its kwargs."""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.json").write_text("[]")
            with patch.object(batch_inference, "inference_jsons") as run:
                result = CliRunner().invoke(
                    batch_inference.mol_cli,
                    ["pred", "-i", str(Path(d) / "a.json"), "-o", d, *options],
                )
        return result, (run.call_args.kwargs if run.called else None)

    def test_unset_options_leave_the_config_alone(self):
        result, kwargs = self.invoke_pred()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(kwargs["memory_settings"], {})

    def test_options_reach_the_runner_by_config_path(self):
        result, kwargs = self.invoke_pred(
            "--infer_setting.chunk_size", "256",
            "--model.msa_module.msa_chunk_size", "512",
            "--infer_setting.diffusion_conditioning_row_chunk_size", "none",
            "--infer_setting.dynamic_chunk_size", "true",
            "--diffusion_cache_mode", "shared",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            kwargs["memory_settings"],
            {
                "infer_setting.chunk_size": 256,
                "model.msa_module.msa_chunk_size": 512,
                "infer_setting.diffusion_conditioning_row_chunk_size": None,
                "infer_setting.dynamic_chunk_size": True,
                "diffusion_cache_mode": "shared",
            },
        )

    def test_a_bad_chunk_size_is_a_usage_error(self):
        for value in ("0", "-4", "big"):
            with self.subTest(value=value):
                result, kwargs = self.invoke_pred("--infer_setting.chunk_size", value)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertIn("--infer_setting.chunk_size", result.output)
                self.assertIsNone(kwargs)

    def test_every_memory_option_is_listed_with_its_default(self):
        result = CliRunner().invoke(batch_inference.mol_cli, ["pred", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        for path in batch_inference.MEMORY_CHUNK_SETTINGS:
            self.assertIn(f"--{path}", result.output)
        self.assertIn("--diffusion_cache_mode", result.output)
        self.assertNotIn("--enable_cache", result.output)

    def test_enable_cache_is_gone(self):
        result, _ = self.invoke_pred("--enable_cache", "false")
        self.assertEqual(result.exit_code, 2, result.output)

    def test_settings_are_applied_to_the_runner_config(self):
        captured = {}

        def build(configs):
            captured["configs"] = configs
            return SimpleNamespace(configs=configs)

        settings = {
            "infer_setting.chunk_size": 256,
            "model.msa_module.msa_chunk_size": 512,
            "infer_setting.diffusion_conditioning_row_chunk_size": None,
            "diffusion_cache_mode": "off",
        }
        with patch.object(batch_inference, "InferenceRunner", side_effect=build), \
             patch.object(batch_inference, "download_inference_cache"):
            batch_inference.get_default_runner(memory_settings=settings)
        configs = captured["configs"]
        self.assertEqual(configs.infer_setting.chunk_size, 256)
        self.assertEqual(configs.model.msa_module.msa_chunk_size, 512)
        self.assertIsNone(configs.infer_setting.diffusion_conditioning_row_chunk_size)
        self.assertEqual(configs.diffusion_cache_mode, "off")
        # Untouched settings keep their inference defaults.
        self.assertEqual(
            configs.infer_setting.trunk_pairformer_transition_chunk_size,
            batch_inference.inference_default(
                "infer_setting.trunk_pairformer_transition_chunk_size"
            ),
        )

    def test_out_of_memory_hint_names_the_mol_pred_options(self):
        hint = inference._failure_hint(torch.cuda.OutOfMemoryError("CUDA out of memory"))
        for option in (
            "--infer_setting.chunk_size",
            "--model.msa_module.msa_chunk_size",
            "--sample",
            "--trimul_kernel torch",
            "expandable_segments:True",
            "docs/inference_memory.md",
        ):
            self.assertIn(option, hint)


if __name__ == "__main__":
    unittest.main()
