# Copyright 2026 Molence Bio
# Licensed under the Apache License, Version 2.0.
import unittest
from types import SimpleNamespace

import torch

from mol.model.modules.pairformer import (
    MSAModule,
    PairformerStack,
)
from mol.model.modules.primitives import Transition
from mol.model.mol import Mol


class PairTransitionChunkTest(unittest.TestCase):
    def setUp(self) -> None:
        torch.manual_seed(7)
        self.transition = Transition(c_in=4, n=2).eval()
        # Keep this CPU test independent of the configured fused LayerNorm
        # backend and avoid the zero-initialized output projection making an
        # ordering bug invisible.
        self.transition.layernorm1 = torch.nn.LayerNorm(4)
        with torch.no_grad():
            for parameter in self.transition.parameters():
                parameter.uniform_(-0.2, 0.2)

    def test_chunked_eval_matches_legacy_and_limits_working_rows(self) -> None:
        x = torch.randn(5, 7, 4)
        expected = self.transition(x)
        for chunk_size in (3, 7, 10):
            with self.subTest(chunk_size=chunk_size):
                seen_rows = []
                hook = self.transition.layernorm1.register_forward_pre_hook(
                    lambda _module, args: seen_rows.append(
                        int(args[0].numel() // args[0].shape[-1])
                    )
                )
                try:
                    actual = self.transition(x, chunk_size=chunk_size)
                finally:
                    hook.remove()
                torch.testing.assert_close(actual, expected)
                if chunk_size < x.shape[-2]:
                    self.assertEqual(len(seen_rows), 3)
                    self.assertLessEqual(max(seen_rows), x.shape[0] * chunk_size)
                else:
                    self.assertEqual(seen_rows, [35])


    def test_invalid_chunk_is_rejected_in_eval(self) -> None:
        x = torch.randn(2, 3, 4)
        for invalid in (0, -1, False, 1.5, "invalid"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    self.transition(x, chunk_size=invalid)

    def test_pairformer_stack_routes_transition_chunks_by_block_type(self) -> None:
        stack = PairformerStack(
            n_blocks=2,
            n_heads=2,
            c_z=4,
            c_s=8,
            n_pairmixer_blocks=1,
            dropout=0.0,
        )
        blocks = stack._prep_blocks(
            pair_mask=None,
            chunk_size=17,
            pairmixer_transition_chunk_size=11,
            pairformer_transition_chunk_size=7,
        )

        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].keywords["chunk_size"], 17)
        self.assertEqual(
            blocks[0].keywords["pair_transition_chunk_size"], 11
        )
        self.assertEqual(blocks[1].keywords["chunk_size"], 17)
        self.assertEqual(
            blocks[1].keywords["pair_transition_chunk_size"], 7
        )

    def test_msa_routes_its_own_transition_chunk(self) -> None:
        module = MSAModule(
            n_blocks=1,
            c_m=4,
            c_z=8,
            c_s_inputs=6,
            msa_configs={"enable": False},
        )
        blocks = module._prep_blocks(
            pair_mask=None,
            chunk_size=19,
            msa_chunk_size=13,
            pair_transition_chunk_size=5,
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].keywords["chunk_size"], 19)
        self.assertEqual(blocks[0].keywords["msa_chunk_size"], 13)
        self.assertEqual(
            blocks[0].keywords["pair_transition_chunk_size"], 5
        )

    def test_stage_override_and_pair_chunk_fallback(self) -> None:
        fake_model = SimpleNamespace(
            configs=SimpleNamespace(
                infer_setting=SimpleNamespace(
                    msa_pair_transition_chunk_size=2048
                )
            )
        )

        self.assertEqual(
            Mol._pair_transition_chunk_size(
                fake_model,
                "msa_pair_transition_chunk_size",
                128,
            ),
            2048,
        )
        fake_model.configs.infer_setting.msa_pair_transition_chunk_size = None
        self.assertEqual(
            Mol._pair_transition_chunk_size(
                fake_model, "msa_pair_transition_chunk_size", 128
            ),
            128,
        )
        self.assertIsNone(
            Mol._pair_transition_chunk_size(
                fake_model, "msa_pair_transition_chunk_size", None
            )
        )


if __name__ == "__main__":
    unittest.main()
