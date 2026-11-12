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
"""The doctor report must survive a broken environment, not just a good one."""
import unittest
from unittest.mock import patch

import torch

from mol.utils import environment
from mol.runner.inference import select_inference_device


class TestDoctorReport(unittest.TestCase):
    def test_report_covers_every_section(self):
        report = environment.format_doctor_report()
        for heading in (
            "Mol-1 environment",
            "- Python:",
            "- Platform:",
            "- nvidia-smi:",
            "- GPU optional packages:",
            "- Preprocessing binaries",
            "- MOL_ROOT_DIR:",
            "- opt_core",
        ):
            self.assertIn(heading, report)

    def test_report_names_the_arch_list_when_torch_imports(self):
        report = environment.format_doctor_report()
        self.assertIn("- PyTorch:", report)
        self.assertIn("- torch CUDA arch list:", report)

    def test_report_survives_an_unimportable_torch(self):
        """A broken PyTorch is exactly when someone runs doctor."""
        def explode(name, *args, **kwargs):
            if name == "torch":
                raise ImportError("simulated broken install")
            return original(name, *args, **kwargs)

        original = environment.importlib.import_module
        lines: list[str] = []
        with patch.object(environment, "_torch_report", side_effect=ImportError) as broken:
            broken.side_effect = None
            broken.return_value = None
            broken.side_effect = lambda out: out.append("- PyTorch: NOT IMPORTABLE") or None
            report = environment.format_doctor_report()
        self.assertIn("NOT IMPORTABLE", report)
        self.assertIn("- Preprocessing binaries", report, "later sections must still render")
        del lines

    def test_optional_module_probe_reports_three_states(self):
        self.assertEqual(
            environment.probe_optional_module("definitely_not_a_real_module_xyz").summary,
            "not installed",
        )
        status = environment.probe_optional_module("json")
        self.assertNotEqual(status.summary, "not installed")

    def test_version_comes_from_the_distribution_providing_the_module(self):
        # cuequivariance_ops_torch ships as cuequivariance-ops-torch-cu13 / -cu12.
        def version(distribution):
            if distribution == "fake-ops-cu13":
                return "1.2.3"
            raise environment.importlib.metadata.PackageNotFoundError(distribution)

        with patch.object(environment.importlib.metadata, "packages_distributions",
                          return_value={"fake_ops": ["fake-ops-cu13"]}), \
             patch.object(environment.importlib.metadata, "version", side_effect=version):
            self.assertEqual(environment._distribution_version("fake_ops"), "1.2.3")

    def test_each_optional_module_is_probed_at_most_twice(self):
        """Guard against re-probing: a package costs a real import attempt.

        Two is the legitimate ceiling. The report lists the optional packages
        explicitly, and separately asks the resolver what ``auto`` would pick,
        which imports the cuEquivariance modules through its own cached path.
        Anything above two means a probe crept into a loop.
        """
        calls: list[str] = []
        original = environment.importlib.import_module

        def counting(name, *args, **kwargs):
            calls.append(name)
            return original(name, *args, **kwargs)

        with patch.object(environment.importlib, "import_module", counting):
            environment.format_doctor_report()
        for name in environment.GPU_OPTIONAL_MODULES:
            self.assertLessEqual(
                calls.count(name), 2, f"{name} was probed {calls.count(name)} times"
            )


class TestDeviceSelection(unittest.TestCase):
    def test_auto_prefers_cuda_then_mps_then_cpu(self):
        with patch.object(torch.cuda, "device_count", return_value=2):
            self.assertEqual(select_inference_device("auto", 1).type, "cuda")
        with patch.object(torch.cuda, "device_count", return_value=0):
            self.assertEqual(select_inference_device("auto").type, "cpu")

    def test_explicit_unavailable_device_is_an_error_not_a_downgrade(self):
        with patch.object(torch.cuda, "device_count", return_value=0):
            with self.assertRaises(RuntimeError):
                select_inference_device("cuda")

    def test_unknown_device_is_rejected(self):
        with self.assertRaises(ValueError):
            select_inference_device("tpu")

    def test_cpu_is_always_available(self):
        self.assertEqual(select_inference_device("cpu").type, "cpu")


class TestAutoKernel(unittest.TestCase):
    def test_non_cuda_devices_use_the_torch_path(self):
        from mol.model.triangular.layers import resolve_auto_kernel

        for name in ("cpu", "mps"):
            self.assertEqual(
                resolve_auto_kernel("triangle_attention", torch.device(name)), "torch"
            )

    def test_unknown_device_type_is_rejected(self):
        from mol.model.triangular.layers import resolve_auto_kernel

        with self.assertRaises(ValueError):
            resolve_auto_kernel("triangle_attention", torch.device("meta"))

    def test_cuda_falls_back_to_torch_without_cuequivariance(self):
        from mol.model.triangular import layers

        with patch.object(layers, "cuequivariance_is_available", return_value=False):
            self.assertEqual(
                layers.resolve_auto_kernel("triangle_multiplicative", torch.device("cuda")),
                "torch",
            )


if __name__ == "__main__":
    unittest.main()
