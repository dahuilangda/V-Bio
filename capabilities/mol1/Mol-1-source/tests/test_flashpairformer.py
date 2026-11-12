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
# Modifications Copyright 2026 Molence Bio

"""Routing, reload safety, residual semantics, and real CUDA equivalence."""
import os
import unittest
from unittest import mock

os.environ.setdefault("LAYERNORM_TYPE", "torch")
import torch

from mol.model.triangular.flashpairformer import (
    _weight_cache, _weights, clear_flashpairformer_cache, flashpairformer_stats,
)
from mol.model.triangular.triangular import (
    TriangleMultiplicationIncoming, TriangleMultiplicationOutgoing,
)


def make_module(c=64, incoming=False, device="cpu"):
    module = (TriangleMultiplicationIncoming if incoming else TriangleMultiplicationOutgoing)(c, c)
    # Released initialization has a zero output projection: use nonzero weights.
    with torch.no_grad():
        for name, param in module.named_parameters():
            if param.ndim == 2:
                param.normal_(0, 0.15)
    return module.to(device).eval()


class TestFlashPairformerRouting(unittest.TestCase):
    def test_cpu_fallback_and_gradients(self):
        m = make_module(16)
        z = torch.randn(7, 7, 16, requires_grad=True)
        a = m(z, triangle_multiplicative="flashpairformer")
        r = m(z, triangle_multiplicative="torch")
        torch.testing.assert_close(a, r, rtol=0, atol=0)
        a.square().sum().backward()
        self.assertIsNotNone(z.grad)
        self.assertGreater(z.grad.abs().sum().item(), 0)

    def test_all_weights_invalidate_packing(self):
        m = make_module()
        for parameter in m.parameters():
            cache = _weight_cache(m, _weights(m), torch.bfloat16)
            cache["sentinel"] = True
            with torch.no_grad():
                parameter.add_(0.01)
            self.assertNotIn("sentinel", _weight_cache(m, _weights(m), torch.bfloat16))

    def test_reload_device_move_and_state_dict(self):
        m = make_module()
        keys = set(m.state_dict())
        _weight_cache(m, _weights(m), torch.bfloat16)["sentinel"] = True
        parent = torch.nn.ModuleDict({"triangle": m})
        parent.load_state_dict(parent.state_dict(), strict=True)
        self.assertNotIn("_flashpairformer_cache", m.__dict__)
        _weight_cache(m, _weights(m), torch.bfloat16)
        m.double()
        self.assertNotIn("_flashpairformer_cache", m.__dict__)
        self.assertEqual(keys, set(m.state_dict()))

    def test_inference_tensor_checkpoint_reload(self):
        with torch.inference_mode():
            m = make_module()
            cache = _weight_cache(m, _weights(m), torch.bfloat16)
            cache["sentinel"] = True
            m.load_state_dict(m.state_dict())
            self.assertNotIn("_flashpairformer_cache", m.__dict__)

    def test_ema_cycles_clear_packing_and_restore_after_failure(self):
        from mol.runner.ema import EMAWrapper

        m = make_module()
        parent = torch.nn.ModuleDict({"triangle": m})
        ema = EMAWrapper(parent)
        ema.register()
        original = {k: v.clone() for k, v in parent.state_dict().items()}
        ema.shadow["triangle.linear_z.weight"].add_(1)
        for fail in (False, True):
            _weight_cache(m, _weights(m), torch.bfloat16)["sentinel"] = True
            ema.apply_shadow()
            self.assertNotIn("_flashpairformer_cache", m.__dict__)
            self.assertNotIn("_flashpairformer_signature", m.__dict__)
            torch.testing.assert_close(
                m.linear_z.weight, original["triangle.linear_z.weight"] + 1
            )
            try:
                _weight_cache(m, _weights(m), torch.bfloat16)["sentinel"] = True
                if fail:
                    raise RuntimeError("evaluation failed")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "evaluation failed")
            finally:
                ema.restore()
            self.assertNotIn("_flashpairformer_cache", m.__dict__)
            self.assertNotIn("_flashpairformer_signature", m.__dict__)
            self.assertEqual(ema.backup, {})
            for name, value in parent.state_dict().items():
                torch.testing.assert_close(value, original[name], rtol=0, atol=0)

    def test_explicit_data_update_clear(self):
        m = make_module()
        _weight_cache(m, _weights(m), torch.bfloat16)
        m.linear_z.weight.data.mul_(2)
        clear_flashpairformer_cache(m)
        self.assertNotIn("_flashpairformer_cache", m.__dict__)


def _opt_core_available() -> bool:
    """Resolve opt_core the same way the kernel does, OPT_CORE_DIR included."""
    try:
        from mol.model.triangular.flashpairformer import _import_trimul

        _import_trimul()
    except Exception:
        return False
    return True


@unittest.skipUnless(
    torch.cuda.is_available()
    and os.getenv("TEST_FLASHPAIRFORMER_CUDA") == "1"
    and _opt_core_available(),
    "needs CUDA, TEST_FLASHPAIRFORMER_CUDA=1, and opt_core installed or OPT_CORE_DIR set",
)
class TestFlashPairformerCUDA(unittest.TestCase):
    def test_cueq_equivalence_mask_layout_dtype_residual(self):
        torch.manual_seed(731)
        for c in (64, 128, 256, 320, 448):
            for incoming in (False, True):
                m = make_module(c, incoming, "cuda")
                for resident in (torch.bfloat16, torch.float32):
                    z = torch.randn(129, 129, c, device="cuda", dtype=resident).transpose(0, 1)
                    mask = (torch.rand(129, 129, device="cuda") > .2).to(resident)
                    for mask0 in (None, mask):
                        for residual in (False, True):
                            with self.subTest(c=c, incoming=incoming, resident=resident, mask=mask0 is not None, residual=residual):
                                before = z.clone()
                                with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
                                    kw = dict(mask=mask0, inplace_safe=residual, _add_with_inplace=residual)
                                    ref = m(z, triangle_multiplicative="cuequivariance", **kw)
                                    got = m(z, triangle_multiplicative="flashpairformer", **kw)
                                self.assertTrue(torch.equal(got, ref), (got.float()-ref.float()).abs().max().item())
                                self.assertTrue(torch.equal(z, before))
                                self.assertEqual(got.dtype, ref.dtype)
                self.assertEqual(flashpairformer_stats(m).get("tmk3_exact"), 8)

    def test_reloaded_weights_are_used(self):
        m = make_module(320, device="cuda")
        z = torch.randn(127, 127, 320, device="cuda", dtype=torch.bfloat16)
        previous = None
        for iteration in range(3):
            if iteration:
                sd = {k: v.clone() for k, v in m.state_dict().items()}
                sd["linear_z.weight"].mul_(1.3)
                sd["layer_norm_in.bias"].add_(0.1)
                m.load_state_dict(sd, strict=True)
            with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
                ref = m(z, triangle_multiplicative="cuequivariance")
                got = m(z, triangle_multiplicative="flashpairformer")
            self.assertTrue(torch.equal(got, ref))
            if iteration:
                self.assertFalse(torch.equal(got, previous))
            previous = got.clone()

    def test_without_autocast_preserves_weight_compute_dtype(self):
        z = torch.randn(129, 129, 320, device="cuda", dtype=torch.bfloat16)
        for weight_dtype in (torch.float32, torch.bfloat16):
            m = make_module(320, device="cuda").to(weight_dtype)
            with torch.inference_mode():
                ref = m(z, triangle_multiplicative="cuequivariance")
                got = m(z, triangle_multiplicative="flashpairformer")
            self.assertTrue(torch.equal(got, ref))
            self.assertEqual(got.dtype, ref.dtype)
            reason = ("tmk3_exact" if weight_dtype == torch.bfloat16
                      else "fallback:requires_bf16_compute")
            self.assertEqual(flashpairformer_stats(m), {reason: 1})

    def test_short_sequences_preserve_cueq_dispatch(self):
        import importlib
        cueq = importlib.import_module("cuequivariance_ops_torch.triangle_multiplicative_update")
        m = make_module(320, device="cuda")
        for n, threshold in ((65, 100), (100, 100), (129, 200)):
            with mock.patch.object(cueq, "CUEQ_TRIMUL_FALLBACK_THRESHOLD", threshold):
                z = torch.randn(n, n, 320, device="cuda", dtype=torch.bfloat16)
                with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
                    ref = m(z, triangle_multiplicative="cuequivariance")
                    got = m(z, triangle_multiplicative="flashpairformer")
                self.assertTrue(torch.equal(got, ref))
        self.assertEqual(flashpairformer_stats(m), {"fallback:cueq_short_sequence": 3})

    def test_training_routes_to_cueq(self):
        m = make_module(64, device="cuda").train()
        z = torch.randn(33, 33, 64, device="cuda", dtype=torch.bfloat16, requires_grad=True)
        with torch.autocast("cuda", dtype=torch.bfloat16):
            y = m(z, triangle_multiplicative="flashpairformer")
            y.float().square().mean().backward()
        self.assertIsNotNone(z.grad)
        self.assertEqual(flashpairformer_stats(m), {"fallback:autograd_or_training": 1})


if __name__ == "__main__":
    unittest.main()
