# Copyright 2026 Molence Bio
# Licensed under the Apache License, Version 2.0.
"""Tests for MSA-stack chunking during training."""

import unittest
from unittest.mock import patch

import torch
import torch.nn as nn

from mol.model.modules import pairformer
from mol.model.modules.pairformer import MSAStack


class MSAStackChunkingTest(unittest.TestCase):

    def test_chunk_forward_uses_chunk_size_not_chunk_count(self):
        class RecordChunkSizes(nn.Module):
            def __init__(self):
                super().__init__()
                self.sizes = []

            def forward(self, m):
                self.sizes.append(m.shape[-3])
                return m

        stack = MSAStack(c_m=4, c_z=8, c=2, dropout=0.0, msa_chunk_size=2)
        module = RecordChunkSizes()
        m = torch.randn(5, 3, 4)
        with patch.object(pairformer, "get_checkpoint_fn", return_value=lambda fn, *args: fn(*args)):
            output = stack.chunk_forward(module, m, z=None, chunk_size=2)
        self.assertEqual(module.sizes, [2, 2, 1])
        torch.testing.assert_close(output, m)
