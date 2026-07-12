# Copyright 2024 ByteDance and/or its affiliates.
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

"""Host-RAM tensor offload for low-VRAM inference.

Large, long-lived pair-stack inputs (z_init, the relative-position encoding, the
O(N^2) template features) dominate VRAM during the Pairformer trunk even though
each is only read in a narrow window of each recycle cycle. Parking them on the
host and prefetching them back with non-blocking copies on a dedicated stream lets
the H2D transfers hide behind compute, cutting the VRAM peak for a small wall-time
cost (the LMI4Boltz technique).

Opt-in via environment, read once on construction:
  PROTENIX_LOW_VRAM  enables offload (the same flag that switches the trunk to
                     chunked kernels and aggressive chunk thresholds).
When disabled the default code path is untouched.

Host-RAM only, by design. An earlier revision also supported a peer GPU as the
offload target (its D2D bandwidth beats H2D in theory), but doing so reserved a
whole second 24 GB card as a tensor warehouse for one task -- a net throughput
loss, since the scheduler could otherwise run another job on that card. With this
design multi-GPU always means data parallelism (one job per card), never a scratch
pad for a single job.
"""

import os
from contextlib import contextmanager
from typing import Iterator

import torch

_TRUTHY = {"1", "true", "yes", "on"}


class TensorOffloader:
    """Parks tensors on the host and prefetches them back asynchronously.

    Args:
        compute_device: the GPU the model runs on. Prefetch copies land here.
    """

    def __init__(self, compute_device: torch.device) -> None:
        low_vram = os.environ.get("PROTENIX_LOW_VRAM", "").strip().lower() in _TRUTHY
        self.compute_device = compute_device
        self.offload_device = torch.device("cpu")
        self.enabled = low_vram and compute_device.type == "cuda"
        # Dedicated stream so prefetch copies overlap the default-stream compute
        # that runs between fetch() and wait().
        self._stream = (
            torch.cuda.Stream(device=compute_device) if self.enabled else None
        )

    def park(self, t: torch.Tensor) -> torch.Tensor:
        """Copy a tensor to page-locked host RAM for the duration of the trunk."""
        return t.to(self.offload_device).pin_memory()

    def fetch(self, parked: torch.Tensor) -> torch.Tensor:
        """Copy a parked tensor back to the compute device on the prefetch stream.

        The result is not readable until ``wait()`` is called. Issue ``fetch``
        early so the copy overlaps with intervening compute on the default stream.
        """
        with torch.cuda.stream(self._stream):
            return parked.to(self.compute_device, non_blocking=True)

    def wait(self) -> None:
        """Block the default stream until every outstanding ``fetch`` has landed."""
        torch.cuda.current_stream().wait_stream(self._stream)

    def restore(self, parked: torch.Tensor, device: torch.device) -> torch.Tensor:
        """Synchronous copy back to ``device`` (one-shot, no overlap needed)."""
        return parked.to(device)

    @contextmanager
    def staged(self, store: dict, keys: list[str]) -> Iterator[None]:
        """Temporarily replace parked ``store`` entries with compute-device copies.

        Fetches are issued and synced on entry, the swap is reverted on exit (the
        compute copies are then freed), so the parked values stay resident on the
        host only for the duration of the block. A no-op when disabled, so
        callers can wrap unconditionally.
        """
        if not self.enabled or not keys:
            yield
            return
        fetched = {k: self.fetch(store[k]) for k in keys}
        self.wait()
        saved = {k: store[k] for k in keys}
        store.update(fetched)
        try:
            yield
        finally:
            for k in keys:
                store[k] = saved[k]
            del fetched  # drop compute-device copies before the caller's next op
