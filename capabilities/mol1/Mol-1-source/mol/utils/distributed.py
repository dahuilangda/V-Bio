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
#
# This file has been modified by Molence Bio
# Based on Protenix: https://github.com/bytedance/Protenix
# Upstream revision: bd54a05d047b8925a241056f36d619700604068a
# Upstream file: protenix/utils/distributed.py

import hashlib
import os
from typing import Optional

import torch

from mol.utils.logger import get_logger

logger = get_logger(__name__)


def distributed_available() -> bool:
    return torch.distributed.is_available() and torch.distributed.is_initialized()


class DistWrapper:
    def __init__(self) -> None:
        self.rank = int(os.environ.get("RANK", 0))
        self.local_rank = int(os.environ.get("LOCAL_RANK", 0))
        self.local_world_size = int(os.environ.get("LOCAL_WORLD_SIZE", 1))
        self.world_size = int(os.environ.get("WORLD_SIZE", 1))
        self.num_nodes = int(self.world_size // self.local_world_size)
        self.node_rank = int(self.rank // self.local_world_size)

    def all_gather_object(self, obj, group=None):
        """Function to gather objects from several distributed processes.
        It is now only used by sync metrics in logger due to security reason.
        """
        if self.world_size > 1 and distributed_available():
            with torch.no_grad():
                obj_list = [None for _ in range(self.world_size)]
                torch.distributed.all_gather_object(obj_list, obj, group=group)
                return obj_list
        else:
            return [obj]


DIST_WRAPPER = DistWrapper()


def assert_replicas_agree(
    model: "torch.nn.Module", sample_parameters: int = 8
) -> Optional[bool]:
    """Check that every rank holds the same parameters, and say so.

    Distributed training is wrong in a way that looks right when gradients are
    not synchronised: every rank keeps stepping, every loss stays finite, and
    the run only differs from the intended one in its result. Ranks start from
    the same weights and see different data, so identical parameters afterwards
    are evidence that the gradients were reduced.

    A digest of a sample of parameters is compared rather than all of them:
    an unsynchronised gradient moves whichever parameters its loss touched, and
    a handful spread across the model is enough to notice while costing a
    fraction of a second.

    Args:
        model (torch.nn.Module): The model to check, wrapped or not.
        sample_parameters (int): How many parameter tensors to digest.

    Returns:
        Optional[bool]: True if the ranks agree, False if they do not, and
            None when there is nothing to compare because this is a single
            process.

    Raises:
        ValueError: If ``sample_parameters`` is not positive.
    """
    if sample_parameters < 1:
        raise ValueError(f"sample_parameters must be positive, got {sample_parameters}")
    if DIST_WRAPPER.world_size <= 1 or not distributed_available():
        return None

    module = getattr(model, "module", model)
    names = sorted(n for n, p in module.named_parameters() if p.numel() > 0)
    if not names:
        return None
    # Spread the sample over the whole parameter list rather than taking a
    # prefix, so a desync confined to one stage is still visible.
    stride = max(1, len(names) // sample_parameters)
    chosen = names[::stride][:sample_parameters]
    tensors = dict(module.named_parameters())

    digest = hashlib.sha256()
    for name in chosen:
        digest.update(name.encode())
        digest.update(tensors[name].detach().float().cpu().numpy().tobytes())
    digests = DIST_WRAPPER.all_gather_object(digest.hexdigest())

    agree = len(set(digests)) == 1
    if agree:
        logger.info(
            "Replica check: all %d ranks hold identical parameters "
            "(%d tensors sampled).",
            DIST_WRAPPER.world_size,
            len(chosen),
        )
    else:
        disagreeing = sorted(
            {i for i, d in enumerate(digests) if d != digests[0]} or {0}
        )
        logger.error(
            "Replica check FAILED: ranks %s differ from rank 0 after training. "
            "Gradients are not being reduced across ranks, so each rank has "
            "trained on its own shard alone and the result is not the intended "
            "one. Check that the model is wrapped in DistributedDataParallel "
            "and that the loss backward goes through that wrapper.",
            disagreeing,
        )
    return agree


def traverse_and_aggregate(dict_list, aggregation_func=None):
    """Traverse list of dicts and merge into a single dict with leaf values joined to list."""
    merged_dict = {}
    all_keys = set().union(*dict_list)
    for key in all_keys:
        agg_value = [m[key] for m in dict_list if key in m]

        if isinstance(agg_value[0], dict):
            merged_dict[key] = traverse_and_aggregate(
                agg_value, aggregation_func=aggregation_func
            )
        else:
            if aggregation_func is not None:
                agg_value = aggregation_func(agg_value)
            merged_dict[key] = agg_value

    return merged_dict


def gather_and_merge(metrics, aggregation_func=None):
    """Gather metrics from ddp workers and aggregate leaf metrics."""
    gathered_metrics = DIST_WRAPPER.all_gather_object(metrics)  # list of metrics
    merged_metrics = traverse_and_aggregate(gathered_metrics, aggregation_func)
    return merged_metrics
