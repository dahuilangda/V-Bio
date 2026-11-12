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

"""Prediction runs without MC dropout or TF32 and one diffusion sample at a
time; evaluation during training keeps its own settings."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from mol.config.base import configs as configs_base
from mol.config.inference import inference_configs
from mol.config.parser import parse_configs
from mol.runner import batch_inference


def parsed(configs):
    return parse_configs(configs=configs, fill_required_with_null=True)


class InferenceDefaultsTest(unittest.TestCase):
    def test_prediction_defaults(self):
        configs = parsed({**configs_base, **inference_configs})
        self.assertEqual(configs.mc_dropout_apply_rate, 0.0)
        self.assertEqual(configs.mc_dropout_rate, 0.0)
        self.assertFalse(configs.enable_tf32)
        self.assertEqual(configs.infer_setting.sample_diffusion_chunk_size, 1)
        # The rest of infer_setting is the shared one.
        self.assertEqual(
            configs.infer_setting.chunk_size,
            configs_base["infer_setting"]["chunk_size"].value,
        )

    def test_mol_pred_leaves_tf32_off(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.json").write_text("[]")
            with patch.object(batch_inference, "inference_jsons") as run:
                result = CliRunner().invoke(
                    batch_inference.mol_cli, ["pred", "-i", str(Path(d) / "a.json"), "-o", d]
                )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(run.call_args.kwargs["enable_tf32"])

    def test_training_evaluation_is_unchanged(self):
        configs = parsed(dict(configs_base))
        self.assertEqual(configs.mc_dropout_apply_rate, 0.4)
        self.assertEqual(configs.infer_setting.sample_diffusion_chunk_size, 5)


if __name__ == "__main__":
    unittest.main()
