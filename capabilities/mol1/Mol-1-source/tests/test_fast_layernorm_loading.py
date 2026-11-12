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
import subprocess
import sys
import unittest
from unittest.mock import patch

import torch

from mol.model.layer_norm import layer_norm
from mol.model.triangular.layers import OpenFoldLayerNorm


class FastLayerNormLoadingTest(unittest.TestCase):
    def test_importing_the_model_builds_nothing(self):
        # A fresh interpreter with the fused LayerNorm selected: importing the
        # model code must not load or compile the extension.
        env = {k: v for k, v in os.environ.items() if k != "LAYERNORM_TYPE"}
        out = subprocess.run(
            [sys.executable, "-c",
             "import mol.model.triangular.layers; "
             "from mol.model.layer_norm import layer_norm; "
             "print(layer_norm._extension_state is None)"],
            capture_output=True, text=True, env=env, check=True,
        )
        self.assertEqual(out.stdout.strip().splitlines()[-1], "True")

    def test_extension_loads_once_on_first_use(self):
        saved = layer_norm._extension_state
        layer_norm._extension_state = None
        try:
            with patch.object(layer_norm.torch.cuda, "is_available", return_value=True), \
                 patch.object(layer_norm, "_load_fast_layer_norm_extension",
                              return_value=("extension", "build info", "build dir")) as load:
                self.assertEqual(layer_norm.fast_layer_norm_cuda, "extension")
                self.assertEqual(layer_norm.FAST_LAYER_NORM_BUILD_INFO, "build info")
                self.assertEqual(layer_norm.FAST_LAYER_NORM_BUILD_DIRECTORY, "build dir")
                layer_norm.FusedLayerNorm(4)
                load.assert_called_once()
        finally:
            layer_norm._extension_state = saved

    def test_construction_triggers_the_load(self):
        saved = layer_norm._extension_state
        layer_norm._extension_state = None
        try:
            with patch.object(layer_norm.torch.cuda, "is_available", return_value=True), \
                 patch.object(layer_norm, "_load_fast_layer_norm_extension",
                              side_effect=RuntimeError("no toolchain")):
                with self.assertRaisesRegex(RuntimeError, "no toolchain"):
                    layer_norm.FusedLayerNorm(4)
        finally:
            layer_norm._extension_state = saved

    def assert_matches_pytorch_path(self, fused):
        reference = OpenFoldLayerNorm(8)
        with torch.no_grad():
            fused.weight.uniform_(0.5, 1.5)
            fused.bias.uniform_(-0.5, 0.5)
        reference.load_state_dict(fused.state_dict())
        x = torch.randn(3, 5, 8)
        for dtype in (torch.float32, torch.bfloat16):
            out = fused(x.to(dtype))
            self.assertEqual(out.dtype, dtype)
            torch.testing.assert_close(out, reference(x.to(dtype)), rtol=0, atol=0)

    def test_without_cuda_nothing_is_built_and_inputs_use_pytorch(self):
        saved = layer_norm._extension_state
        layer_norm._extension_state = None
        try:
            with patch.object(layer_norm.torch.cuda, "is_available", return_value=False), \
                 patch.object(layer_norm, "_load_fast_layer_norm_extension",
                              side_effect=AssertionError("built without CUDA")):
                fused = layer_norm.FusedLayerNorm(8)
            self.assert_matches_pytorch_path(fused)
        finally:
            layer_norm._extension_state = saved

    def test_inputs_off_cuda_never_reach_the_cuda_kernel(self):
        # A GPU machine running on --device cpu: the extension is loaded, but
        # CPU tensors must not be handed to the CUDA kernel.
        with patch.object(layer_norm.torch.cuda, "is_available", return_value=True), \
             patch.object(layer_norm, "_fast_layer_norm_extension", return_value="extension"):
            fused = layer_norm.FusedLayerNorm(8)
        with patch.object(layer_norm.FusedLayerNormAffineFunction, "apply",
                          side_effect=AssertionError("CUDA kernel called")):
            self.assert_matches_pytorch_path(fused)

    def test_unknown_attribute_is_still_an_error(self):
        with self.assertRaises(AttributeError):
            layer_norm.not_an_attribute  # noqa: B018


if __name__ == "__main__":
    unittest.main()
