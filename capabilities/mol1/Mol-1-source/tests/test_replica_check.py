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

"""The end-of-training check that every rank holds the same parameters.

Unsynchronised gradients make a distributed run look healthy the whole way
through and simply produce the wrong result, so the check exists to turn that
into a message. These tests drive it with a stubbed gather, since the real one
needs a process group.
"""

import unittest
from unittest import mock

import torch

from mol.utils import distributed as dist_utils


def tiny_model(fill: float = 1.0) -> torch.nn.Module:
    model = torch.nn.Sequential(
        torch.nn.Linear(4, 4), torch.nn.Linear(4, 4), torch.nn.Linear(4, 2)
    )
    with torch.no_grad():
        for parameter in model.parameters():
            parameter.fill_(fill)
    return model


class FakeWrapper:
    """Stands in for DIST_WRAPPER, returning digests the test chooses."""

    def __init__(self, world_size: int, digests=None):
        self.world_size = world_size
        self._digests = digests

    def all_gather_object(self, obj, group=None):
        if self._digests is None:
            return [obj] * self.world_size
        return list(self._digests)


class TestAssertReplicasAgree(unittest.TestCase):
    def test_single_process_has_nothing_to_compare(self):
        with mock.patch.object(dist_utils, "DIST_WRAPPER", FakeWrapper(1)):
            self.assertIsNone(dist_utils.assert_replicas_agree(tiny_model()))

    def test_unavailable_process_group_is_not_an_error(self):
        with mock.patch.object(dist_utils, "DIST_WRAPPER", FakeWrapper(8)), \
             mock.patch.object(dist_utils, "distributed_available", lambda: False):
            self.assertIsNone(dist_utils.assert_replicas_agree(tiny_model()))

    def test_matching_digests_agree(self):
        with mock.patch.object(dist_utils, "DIST_WRAPPER", FakeWrapper(8)), \
             mock.patch.object(dist_utils, "distributed_available", lambda: True):
            self.assertIs(dist_utils.assert_replicas_agree(tiny_model()), True)

    def test_one_divergent_rank_is_reported(self):
        """A single disagreeing rank must fail the check, not be averaged away."""
        digests = ["same"] * 8
        digests[5] = "different"
        with mock.patch.object(dist_utils, "DIST_WRAPPER", FakeWrapper(8, digests)), \
             mock.patch.object(dist_utils, "distributed_available", lambda: True), \
             self.assertLogs(dist_utils.logger, level="ERROR") as logs:
            self.assertIs(dist_utils.assert_replicas_agree(tiny_model()), False)
        self.assertIn("Replica check FAILED", "\n".join(logs.output))
        self.assertIn("[5]", "\n".join(logs.output))

    def test_it_reads_through_a_ddp_style_wrapper(self):
        """Called on the wrapped model, it must digest the wrapped parameters."""

        class Wrapped(torch.nn.Module):
            def __init__(self, module):
                super().__init__()
                self.module = module

        inner = tiny_model()
        captured = {}

        def capture(obj, group=None):
            captured["digest"] = obj
            return [obj] * 8

        wrapper = FakeWrapper(8)
        wrapper.all_gather_object = capture
        with mock.patch.object(dist_utils, "DIST_WRAPPER", wrapper), \
             mock.patch.object(dist_utils, "distributed_available", lambda: True):
            dist_utils.assert_replicas_agree(Wrapped(inner))
            wrapped_digest = captured["digest"]
            dist_utils.assert_replicas_agree(inner)
        self.assertEqual(wrapped_digest, captured["digest"])

    def test_differing_weights_produce_differing_digests(self):
        """Otherwise the digest would not detect anything."""
        seen = []

        def capture(obj, group=None):
            seen.append(obj)
            return [obj] * 8

        wrapper = FakeWrapper(8)
        wrapper.all_gather_object = capture
        with mock.patch.object(dist_utils, "DIST_WRAPPER", wrapper), \
             mock.patch.object(dist_utils, "distributed_available", lambda: True):
            dist_utils.assert_replicas_agree(tiny_model(1.0))
            dist_utils.assert_replicas_agree(tiny_model(1.5))
        self.assertNotEqual(seen[0], seen[1])

    def test_sample_size_must_be_positive(self):
        with mock.patch.object(dist_utils, "DIST_WRAPPER", FakeWrapper(8)), \
             mock.patch.object(dist_utils, "distributed_available", lambda: True):
            with self.assertRaises(ValueError):
                dist_utils.assert_replicas_agree(tiny_model(), sample_parameters=0)


if __name__ == "__main__":
    unittest.main()
