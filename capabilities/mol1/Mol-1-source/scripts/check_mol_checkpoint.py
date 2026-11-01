#!/usr/bin/env python3
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
"""Check that the Mol-1 architecture strictly accepts a local checkpoint.

This is a validation tool. Meta-device construction and mmap avoid allocating
another full copy of the checkpoint; inference and training do not use them.
"""

import argparse
import os
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LAYERNORM_TYPE", "torch")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch

from mol.config.base import configs as base_configs
from mol.config.data import data_configs
from mol.config.inference import inference_configs
from mol.config.parser import parse_configs
from mol.model.mol import Mol


@contextmanager
def _meta_model_construction():
    """Keep the constructor's scalar dropout schedule on CPU for ``.item()``."""

    original_linspace = torch.linspace

    def scalar_linspace(*args, **kwargs):
        kwargs.setdefault("device", "cpu")
        return original_linspace(*args, **kwargs)

    with patch.object(torch, "linspace", scalar_linspace), torch.device("meta"):
        yield


def _default_checkpoint() -> str:
    """Same resolution order as the inference runner."""

    return base_configs["load_checkpoint_path"] or os.path.join(
        inference_configs["load_checkpoint_dir"],
        f"{inference_configs['model_name']}.pt",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", default=_default_checkpoint())
    args = parser.parse_args()
    if not os.path.isfile(args.checkpoint):
        raise FileNotFoundError(
            f"Mol-1 checkpoint not found [{args.checkpoint}]. "
            "Pass --checkpoint, or place the weights at that path."
        )

    config_tree = {**base_configs, "data": data_configs, **inference_configs}
    configs = parse_configs(config_tree, fill_required_with_null=True)
    with _meta_model_construction():
        model = Mol(configs)

    checkpoint = torch.load(
        args.checkpoint, map_location="cpu", mmap=True, weights_only=True
    )
    if not isinstance(checkpoint, Mapping):
        raise ValueError("Checkpoint must contain a model state mapping")
    state = checkpoint["model"]
    first_key = next(iter(state))
    if first_key.startswith("module."):
        state = {key[len("module.") :]: value for key, value in state.items()}
    model.load_state_dict(state, strict=True, assign=True)
    print(f"Strict checkpoint load succeeded: {len(state)} state entries")


if __name__ == "__main__":
    main()
