# Copyright 2026 Molence Bio
# Licensed under the Apache License, Version 2.0.
import unittest

import torch

from mol.model.modules.diffusion import DiffusionConditioning
from mol.utils.torch_utils import autocasting_disable_decorator


class DiffusionConditioningChunkTest(unittest.TestCase):
    def setUp(self) -> None:
        torch.manual_seed(7)
        self.module = DiffusionConditioning(
            c_z=4,
            c_s=8,
            c_s_inputs=9,
            c_noise_embedding=8,
        ).eval()
        self.relp = torch.randn(5, 5, 139)
        self.z = torch.randn(5, 5, 4)

    def test_cpu_chunk_matches_legacy_path(self) -> None:
        with torch.no_grad():
            expected = self.module.prepare_cache(self.relp, self.z)
            actual = self.module.prepare_cache(
                self.relp,
                self.z,
                row_chunk_size=2,
            )
        torch.testing.assert_close(actual, expected, rtol=1e-6, atol=1e-6)
        self.assertEqual(actual.device, self.z.device)
        self.assertEqual(actual.dtype, torch.float32)

    def test_chunked_path_is_inference_only(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "inference-only"):
            self.module.prepare_cache(
                self.relp,
                self.z,
                row_chunk_size=2,
            )

    def test_invalid_chunk_options(self) -> None:
        with torch.no_grad():
            for chunk_size in (0, -1, True):
                with self.subTest(chunk_size=chunk_size), self.assertRaisesRegex(ValueError, "positive integer"):
                    self.module.prepare_cache(self.relp, self.z, row_chunk_size=chunk_size)

    @unittest.skipUnless(torch.cuda.is_available(), "CUDA is required")
    def test_cuda_chunk_matches_skip_amp_path(self) -> None:
        module = self.module.cuda()
        relp = self.relp.cuda()
        z = self.z.to(device="cuda", dtype=torch.bfloat16)
        with torch.no_grad():
            expected = autocasting_disable_decorator(True)(module.prepare_cache)(
                relp, z
            )
            cuda_output = module.prepare_cache(
                relp,
                z,
                row_chunk_size=2,
            )
        torch.testing.assert_close(cuda_output, expected, rtol=1e-5, atol=1e-5)
        self.assertEqual(cuda_output.device, z.device)
        self.assertEqual(cuda_output.dtype, torch.float32)


if __name__ == "__main__":
    unittest.main()
