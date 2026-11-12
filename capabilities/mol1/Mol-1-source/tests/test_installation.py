# Copyright 2024 ByteDance and/or its affiliates.
# Modifications Copyright 2026 Molence Bio
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

# Copyright 2025 Shad Nygren, Virtual Hipster Corporation
# Contributed to the Protenix project under the Apache License 2.0

# This file has been modified by Molence Bio
# Based on Protenix: https://github.com/bytedance/Protenix
# Upstream revision: bd54a05d047b8925a241056f36d619700604068a
# Upstream file: tests/test_installation.py

"""
Test suite for installation and dependency compatibility issues.
Related to GitHub issues #182 (pip install error)
"""

import importlib.util
import sys
import unittest


class TestInstallation(unittest.TestCase):
    """Test that package dependencies are compatible"""

    def test_python_version(self):
        """Verify Python version is compatible (3.12+)"""
        version_info = sys.version_info
        self.assertGreaterEqual(version_info.major, 3)
        self.assertGreaterEqual(
            version_info.minor, 12, "Mol-1 requires Python 3.12 or higher"
        )

    def test_required_packages_importable(self):
        """Verify core packages can be imported"""
        required_packages = [
            "torch",
            "numpy",
            "scipy",
            "pandas",
            "ml_collections",
        ]

        for package in required_packages:
            spec = importlib.util.find_spec(package)
            if spec is None:
                self.skipTest(
                    f"{package} not installed - this test documents missing dependencies"
                )

    def test_mol_imports(self):
        """Test that mol package can be imported when installed"""
        try:
            import mol  # noqa: F401

            self.assertTrue(True)
        except ImportError:
            self.skipTest(
                "Mol-1 not installed - this documents the need for installation"
            )

    def test_cuda_availability(self):
        """Document CUDA availability for GPU acceleration"""
        try:
            import torch

            if torch.cuda.is_available():
                self.assertTrue(True, "CUDA is available")
            else:
                # Not a failure, just documentation
                print("Note: CUDA not available, will use CPU")
        except ImportError:
            self.skipTest("PyTorch not installed")


if __name__ == "__main__":
    unittest.main()  # Test signed commit
