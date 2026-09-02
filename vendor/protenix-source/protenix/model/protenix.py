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

import copy
import os
import random
import time
from typing import Any, Optional

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from protenix.model import sample_confidence
from protenix.model.generator import (
    InferenceNoiseScheduler,
    sample_diffusion,
    sample_diffusion_training,
    TrainingNoiseSampler,
)
from protenix.model.modules.confidence import ConfidenceHead
from protenix.model.modules.diffusion import DiffusionModule
from protenix.model.modules.embedders import (
    ConstraintEmbedder,
    InputFeatureEmbedder,
    RelativePositionEncoding,
)
from protenix.model.modules.head import DistogramHead
from protenix.model.modules.pairformer import (
    MSAModule,
    PairformerStack,
    TemplateEmbedder,
)


def _load_p2d_side_channels():
    """Load protenix2dock side-channel files (env-driven, same pattern as PROTENIX_LOW_VRAM).

    Returns a dict with optional entries:
      coords [N_atom, 3] float32, mask [N_atom] float32, noise_scale float,
      pin [N_atom] float32 (atoms clamped to coords every diffusion step),
      score_only bool, contacts {index [M,2] int64, upper [M] float32}.
    Results are cached per (path, mtime) so the multi-seed loop loads them once.
    """
    cache = getattr(_load_p2d_side_channels, "_cache", None)
    key = (
        os.environ.get("PROTENIX_INIT_COORDS_PATH", ""),
        os.environ.get("PROTENIX_TFG_CONTACTS_PATH", ""),
        os.environ.get("PROTENIX_SCORE_ONLY", ""),
        os.environ.get("PROTENIX_PIN_MASK_PATH", ""),
        os.environ.get("PROTENIX_ANCHOR_PAIRS_PATH", ""),
        os.environ.get("PROTENIX_COVALENT_BONDS_PATH", ""),
    )
    if cache is not None and cache[0] == key:
        return cache[1]
    out: dict[str, Any] = {}
    coords_path = os.environ.get("PROTENIX_INIT_COORDS_PATH", "").strip()
    if coords_path and os.path.exists(coords_path):
        blob = np.load(coords_path, allow_pickle=False)
        out["coords"] = np.asarray(blob["coords"], dtype=np.float32)
        out["mask"] = (
            np.asarray(blob["mask"], dtype=np.float32)
            if "mask" in blob.files
            else np.ones(out["coords"].shape[0], dtype=np.float32)
        )
        pin_path = os.environ.get("PROTENIX_PIN_MASK_PATH", "").strip()
        if pin_path and os.path.exists(pin_path):
            pin_blob = np.load(pin_path, allow_pickle=False)
            pin = np.asarray(pin_blob["pin"], dtype=np.float32)
            # coords may be [N_atom, 3] or an ensemble [n_sample, N_atom, 3];
            # the pinned geometry is per-atom, shared by every sample
            n_atom = (
                out["coords"].shape[0] if out["coords"].ndim == 2
                else out["coords"].shape[1]
            )
            if pin.shape[0] == n_atom:
                out["pin"] = pin
                anchor_path = os.environ.get("PROTENIX_ANCHOR_PAIRS_PATH", "").strip()
                if anchor_path and os.path.exists(anchor_path):
                    a_blob = np.load(anchor_path, allow_pickle=False)
                    a_idx = np.asarray(a_blob["pair_index"], dtype=np.int64)
                    a_up = np.asarray(a_blob["upper"], dtype=np.float32)
                    a_lo = (
                        np.asarray(a_blob["lower"], dtype=np.float32)
                        if "lower" in a_blob.files
                        else np.zeros_like(a_up)
                    )
                    if a_idx.ndim == 2 and a_idx.shape[1] == 2 and a_idx.shape[0] == a_up.shape[0]:
                        # producers write [M, 2]; the sampler consumes [2, M]
                        out["anchor_index"] = a_idx.T.copy()
                        out["anchor_upper"] = a_up
                        out["anchor_lower"] = a_lo
                    else:
                        logger.warning(
                            "protenix2dock anchor pairs malformed (pair_index "
                            "must be [M,2] matching upper [M]); ignoring anchors."
                        )
            else:
                logger.warning(
                    f"protenix2dock pin mask N={pin.shape[0]} does not match "
                    f"init coords N={out['coords'].shape[0]}; ignoring pin mask."
                )
    out["score_only"] = os.environ.get("PROTENIX_SCORE_ONLY", "").strip().lower() in {
        "1", "true", "yes", "on",
    } and "coords" in out
    contacts_path = os.environ.get("PROTENIX_TFG_CONTACTS_PATH", "").strip()
    if contacts_path and os.path.exists(contacts_path):
        blob = np.load(contacts_path, allow_pickle=False)
        out["contacts"] = {
            "index": np.asarray(blob["pair_index"], dtype=np.int64),
            "upper": np.asarray(blob["upper"], dtype=np.float32),
        }
    cov_path = os.environ.get("PROTENIX_COVALENT_BONDS_PATH", "").strip()
    if cov_path and os.path.exists(cov_path):
        # Free-chain covalent bonds as hard bands: the pocket/clash
        # projection corrects single atoms, so the bond bands ride the same
        # sweep to distribute every correction through the bond network.
        blob = np.load(cov_path, allow_pickle=False)
        c_idx = np.asarray(blob["pair_index"], dtype=np.int64)
        c_up = np.asarray(blob["upper"], dtype=np.float32)
        c_lo = (
            np.asarray(blob["lower"], dtype=np.float32)
            if "lower" in blob.files
            else np.zeros_like(c_up)
        )
        if c_idx.ndim == 2 and c_idx.shape[1] == 2 and c_idx.shape[0] == c_up.shape[0]:
            # producers write [M, 2]; the sampler consumes [2, M]
            out["cov_index"] = c_idx.T.copy()
            out["cov_upper"] = c_up
            out["cov_lower"] = c_lo
        else:
            logger.warning(
                "protenix2dock covalent bonds malformed (pair_index must be "
                "[M,2] matching upper [M]); ignoring covalent bands."
            )
    _load_p2d_side_channels._cache = (key, out)
    return out


def _p2d_tensor(p2d: dict[str, Any], key: str) -> Any:
    """Optional numpy entry of the p2d side channels as a torch tensor."""
    if p2d.get(key) is None:
        return None
    import torch

    return torch.from_numpy(np.asarray(p2d[key]))


def _build_chiral_quads(input_feature_dict, init_coords, pin):
    """Backbone chirality guard inputs from the model's own input features.

    Groups atoms into residues via atom_to_token_idx (one token per residue
    for polymers), decodes atom names from ref_atom_name_chars (ord(c)-32
    one-hot), and records (N, CA, C, CB) index quads with the DESIGNED
    chirality sign computed from the init coordinates. Residues anchored on
    pinned (receptor) atoms are excluded — they never move. A residue
    without usable init geometry (zero rows) takes the L (+1) design
    contract instead of being skipped. Returns
    ([4, M] int64 tensor, [M] float32 tensor) or (None, None).
    """
    import torch

    if init_coords is None:
        return None, None
    _raw = (
        init_coords.detach().cpu().float().numpy()
        if hasattr(init_coords, "detach")
        else init_coords
    )
    coords = np.asarray(_raw, dtype=np.float32)
    if coords.ndim == 3:
        # per-sample ensemble start; the pinned/free split and the designed
        # chirality are identical across samples by contract
        coords = coords[0]
    pin_col = (
        np.asarray(
            pin.detach().cpu().float().numpy()
            if hasattr(pin, "detach")
            else pin,
            dtype=np.float32,
        )
        if pin is not None
        else np.zeros(coords.shape[0], dtype=np.float32)
    )
    a2t = input_feature_dict["atom_to_token_idx"]
    if a2t.dim() >= 2:
        a2t = a2t[0] if a2t.shape[0] == 1 else a2t.argmax(dim=-1)
    token_of = np.asarray(a2t.detach().cpu().long().numpy()).flatten()

    chars = input_feature_dict["ref_atom_name_chars"]
    if chars.dim() >= 3:
        chars = chars[0] if chars.shape[0] == 1 else chars
    codes = chars.detach().cpu().argmax(dim=-1).numpy()  # [N_atom, 4]
    names = []
    for row in codes:
        name = "".join(chr(int(c) + 32) for c in row).strip()
        names.append(name)

    groups: dict[int, dict[str, int]] = {}
    for i, name in enumerate(names):
        groups.setdefault(int(token_of[i]), {})[name] = i

    quads: list[list[int]] = []
    signs: list[float] = []
    for _, atoms in groups.items():
        needed = ("N", "CA", "C", "CB")
        if not all(nm in atoms for nm in needed):
            continue
        i_n, i_ca, i_c, i_cb = (atoms[nm] for nm in needed)
        if pin_col[i_ca] > 0.5:
            continue
        n = coords[i_n] - coords[i_ca]
        c = coords[i_c] - coords[i_ca]
        cb = coords[i_cb] - coords[i_ca]
        vol = float(np.dot(np.cross(n, c), cb))
        if abs(vol) < 1e-3:
            # no usable init geometry (unmatched residue -> zero rows): the
            # design contract for free protein residues is L (+) in the
            # staged frame — enforce it instead of leaving the residue
            # unguarded (a silently skipped residue ends diffusion with
            # arbitrary stereochemistry)
            vol = 1.0
        quads.append([i_n, i_ca, i_c, i_cb])
        signs.append(1.0 if vol > 0 else -1.0)
    if not quads:
        return None, None
    return (
        torch.tensor(quads, dtype=torch.long).T.contiguous(),
        torch.tensor(signs, dtype=torch.float32),
    )
from protenix.model.modules.primitives import LinearNoBias
from protenix.model.triangular.layers import LayerNorm
from protenix.model.utils import simple_merge_dict_list
from protenix.utils.logger import get_logger
from protenix.utils.offload import TensorOffloader
from protenix.utils.permutation.permutation import SymmetricPermutation
from protenix.utils.torch_utils import autocasting_disable_decorator

logger = get_logger(__name__)


def update_input_feature_dict(input_feature_dict: dict[str, Any]) -> dict[str, Any]:
    """
    Lines 1-3 of Algorithm 5 compute d_lm, v_lm, and pad_info utilized in the AtomAttentionEncoder.
    Args:
            input_feature_dict (dict[str, Any]): input features
    Returns:
            input_feature_dict (dict[str, Any]): input features
    """
    from protenix.model.modules.transformer import rearrange_qk_to_dense_trunk

    with torch.no_grad():
        # Prepare tensors in dense trunks for local operations
        q_trunked_list, k_trunked_list, pad_info = rearrange_qk_to_dense_trunk(
            q=[input_feature_dict["ref_pos"], input_feature_dict["ref_space_uid"]],
            k=[input_feature_dict["ref_pos"], input_feature_dict["ref_space_uid"]],
            dim_q=[-2, -1],
            dim_k=[-2, -1],
            n_queries=32,
            n_keys=128,
            compute_mask=True,
        )
        # Compute atom pair feature
        d_lm = (
            q_trunked_list[0][..., None, :] - k_trunked_list[0][..., None, :, :]
        )  # [..., n_blocks, n_queries, n_keys, 3]
        v_lm = (
            q_trunked_list[1][..., None].int() == k_trunked_list[1][..., None, :].int()
        ).unsqueeze(
            dim=-1
        )  # [..., n_blocks, n_queries, n_keys, 1]
        input_feature_dict["d_lm"] = d_lm
        input_feature_dict["v_lm"] = v_lm
        input_feature_dict["pad_info"] = pad_info
        return input_feature_dict


class Protenix(nn.Module):
    """
    Implements Algorithm 1 [Main Inference/Train Loop] in AF3
    """

    def __init__(self, configs: Any) -> None:
        super(Protenix, self).__init__()
        self.configs = configs
        torch.backends.cuda.matmul.allow_tf32 = self.configs.enable_tf32
        # Some constants
        self.enable_diffusion_shared_vars_cache = (
            self.configs.enable_diffusion_shared_vars_cache
        )
        self.enable_efficient_fusion = self.configs.enable_efficient_fusion
        self.N_cycle = self.configs.model.N_cycle
        self.N_model_seed = self.configs.model.N_model_seed
        self.train_confidence_only = configs.train_confidence_only
        if self.train_confidence_only:  # the final finetune stage
            assert configs.loss.weight.alpha_diffusion == 0.0
            assert configs.loss.weight.alpha_distogram == 0.0

        # Diffusion scheduler
        self.train_noise_sampler = TrainingNoiseSampler(**configs.train_noise_sampler)
        self.inference_noise_scheduler = InferenceNoiseScheduler(
            **configs.inference_noise_scheduler
        )
        self.diffusion_batch_size = self.configs.diffusion_batch_size

        # Model
        esm_configs = configs.get("esm", {})  # This is used in InputFeatureEmbedder
        self.input_embedder = InputFeatureEmbedder(
            **configs.model.input_embedder, esm_configs=esm_configs
        )
        self.relative_position_encoding = RelativePositionEncoding(
            **configs.model.relative_position_encoding
        )
        self.template_embedder = TemplateEmbedder(**configs.model.template_embedder)
        self.msa_module = MSAModule(
            **configs.model.msa_module,
            msa_configs=configs.data.get("msa", {}),
        )
        self.constraint_embedder = ConstraintEmbedder(
            **configs.model.constraint_embedder
        )
        self.pairformer_stack = PairformerStack(**configs.model.pairformer)
        self.diffusion_module = DiffusionModule(**configs.model.diffusion_module)
        self.distogram_head = DistogramHead(**configs.model.distogram_head)
        self.confidence_head = ConfidenceHead(**configs.model.confidence_head)
        # protenix2dock native affinity head: env-driven side channel
        # (PROTENIX_AFFINITY_CKPT), trained by
        # capabilities/protenix2dock/train_affinity.py. Construction is
        # deferred to first use so the stock Protenix checkpoint loads with
        # strict=True before the head's keys exist.
        self.affinity_heads = None
        self._affinity_ckpt_path = os.environ.get("PROTENIX_AFFINITY_CKPT", "").strip()

        self.c_s, self.c_z, self.c_s_inputs = (
            configs.c_s,
            configs.c_z,
            configs.c_s_inputs,
        )
        self.linear_no_bias_sinit = LinearNoBias(
            in_features=self.c_s_inputs, out_features=self.c_s
        )
        self.linear_no_bias_zinit1 = LinearNoBias(
            in_features=self.c_s, out_features=self.c_z
        )
        self.linear_no_bias_zinit2 = LinearNoBias(
            in_features=self.c_s, out_features=self.c_z
        )
        self.linear_no_bias_token_bond = LinearNoBias(
            in_features=1, out_features=self.c_z
        )
        self.linear_no_bias_z_cycle = LinearNoBias(
            in_features=self.c_z, out_features=self.c_z
        )
        self.linear_no_bias_s = LinearNoBias(
            in_features=self.c_s, out_features=self.c_s
        )
        self.layernorm_z_cycle = LayerNorm(self.c_z)
        self.layernorm_s = LayerNorm(self.c_s)

        # Zero init the recycling layer
        nn.init.zeros_(self.linear_no_bias_z_cycle.weight)
        nn.init.zeros_(self.linear_no_bias_s.weight)

    def get_pairformer_output(
        self,
        input_feature_dict: dict[str, Any],
        N_cycle: int,
        inplace_safe: bool = False,
        chunk_size: Optional[int] = None,
        mc_dropout: bool = False,
        mc_dropout_rate: float = 0.4,
    ) -> tuple[torch.Tensor, ...]:
        """
        The forward pass from the input to pairformer output

        Args:
            input_feature_dict (dict[str, Any]): input features
            N_cycle (int): number of cycles
            inplace_safe (bool): Whether it is safe to use inplace operations. Defaults to False.
            chunk_size (Optional[int]): Chunk size for memory-efficient operations. Defaults to None.

        Returns:
            Tuple[torch.Tensor, ...]: s_inputs, s, z
        """
        if self.train_confidence_only:
            self.input_embedder.eval()
            self.template_embedder.eval()
            self.msa_module.eval()
            self.pairformer_stack.eval()

        # Line 1-5
        s_inputs = self.input_embedder(
            input_feature_dict, inplace_safe=False, chunk_size=chunk_size
        )  # [..., N_token, 449]
        z_constraint = None

        if "constraint_feature" in input_feature_dict:
            z_constraint = self.constraint_embedder(
                input_feature_dict["constraint_feature"]
            )

        s_init = self.linear_no_bias_sinit(s_inputs)  # [..., N_token, c_s]
        z_init = (
            self.linear_no_bias_zinit1(s_init)[..., None, :]
            + self.linear_no_bias_zinit2(s_init)[..., None, :, :]
        )  # [..., N_token, N_token, c_z]
        if inplace_safe:
            z_init += self.relative_position_encoding(input_feature_dict["relp"])
            z_init += self.linear_no_bias_token_bond(
                input_feature_dict["token_bonds"].unsqueeze(dim=-1)
            )
            if z_constraint is not None:
                z_init += z_constraint
        else:
            z_init = z_init + self.relative_position_encoding(
                input_feature_dict["relp"]
            )
            z_init = z_init + self.linear_no_bias_token_bond(
                input_feature_dict["token_bonds"].unsqueeze(dim=-1)
            )
            if z_constraint is not None:
                z_init = z_init + z_constraint
        # Line 6
        z = torch.zeros_like(z_init)
        s = torch.zeros_like(s_init)

        # Low-VRAM trunk offload (opt-in via PROTENIX_LOW_VRAM): z_init is read-only and reused
        # every cycle, relp is dead until diffusion conditioning, and the O(N^2) template features
        # are only read inside template_embedder. Park all three on the host so they no longer
        # count against the Pairformer's VRAM peak, then prefetch them back on a side stream that
        # overlaps compute. See protenix.utils.offload -- host-only by design (a peer GPU would
        # cost a whole card to warehouse a few tensors for one job).
        offloader = TensorOffloader(z.device)
        relp_device = input_feature_dict["relp"].device
        tpl_keys: list[str] = []
        if offloader.enabled:
            z_init = offloader.park(z_init)
            input_feature_dict["relp"] = offloader.park(input_feature_dict["relp"])
            if self.template_embedder.n_blocks > 0:
                tpl_keys = [
                    k
                    for k, v in input_feature_dict.items()
                    if k.startswith("template_") and isinstance(v, torch.Tensor)
                ]
                for k in tpl_keys:
                    input_feature_dict[k] = offloader.park(input_feature_dict[k])

        # Line 7-13 recycling
        for cycle_no in range(N_cycle):
            with torch.set_grad_enabled(
                self.training
                and (not self.train_confidence_only)
                and cycle_no == (N_cycle - 1)
            ):
                # Issue the z_init prefetch first so it overlaps the recycle compute below.
                z_init_dev = offloader.fetch(z_init) if offloader.enabled else z_init
                z_cycle = self.linear_no_bias_z_cycle(self.layernorm_z_cycle(z))
                if mc_dropout:
                    z_cycle = F.dropout(z_cycle, p=self.configs.mc_dropout_rate)
                if offloader.enabled:
                    offloader.wait()
                z = z_init_dev + z_cycle
                # Drop the recycle temps now: they're dead for the rest of the cycle but would
                # otherwise sit on the GPU through the template_embedder / pairformer peaks
                # (~2.4 GB each at c_z=256). In the default path z_init_dev is just an alias onto
                # the persistent z_init, so deleting the name frees nothing there.
                del z_init_dev, z_cycle
                if inplace_safe:
                    if self.template_embedder.n_blocks > 0:
                        with offloader.staged(input_feature_dict, tpl_keys):
                            z += self.template_embedder(
                                input_feature_dict,
                                z,
                                triangle_multiplicative=self.configs.triangle_multiplicative,
                                triangle_attention=self.configs.triangle_attention,
                                inplace_safe=inplace_safe,
                                chunk_size=chunk_size,
                            )
                    z = self.msa_module(
                        input_feature_dict,
                        z,
                        s_inputs,
                        pair_mask=None,
                        triangle_multiplicative=self.configs.triangle_multiplicative,
                        triangle_attention=self.configs.triangle_attention,
                        inplace_safe=inplace_safe,
                        chunk_size=chunk_size,
                    )
                else:
                    if self.template_embedder.n_blocks > 0:
                        with offloader.staged(input_feature_dict, tpl_keys):
                            z = z + self.template_embedder(
                                input_feature_dict,
                                z,
                                triangle_multiplicative=self.configs.triangle_multiplicative,
                                triangle_attention=self.configs.triangle_attention,
                                inplace_safe=inplace_safe,
                                chunk_size=chunk_size,
                            )
                    z = self.msa_module(
                        input_feature_dict,
                        z,
                        s_inputs,
                        pair_mask=None,
                        triangle_multiplicative=self.configs.triangle_multiplicative,
                        triangle_attention=self.configs.triangle_attention,
                        inplace_safe=inplace_safe,
                        chunk_size=chunk_size,
                    )
                s = s_init + self.linear_no_bias_s(self.layernorm_s(s))
                s, z = self.pairformer_stack(
                    s,
                    z,
                    pair_mask=None,
                    triangle_multiplicative=self.configs.triangle_multiplicative,
                    triangle_attention=self.configs.triangle_attention,
                    inplace_safe=inplace_safe,
                    chunk_size=chunk_size,
                )

        if self.train_confidence_only:
            self.input_embedder.train()
            self.template_embedder.train()
            self.msa_module.train()
            self.pairformer_stack.train()

        if offloader.enabled:
            # relp is consumed again by diffusion conditioning after the trunk.
            input_feature_dict["relp"] = offloader.restore(
                input_feature_dict["relp"], relp_device
            )

        return s_inputs, s, z

    def sample_diffusion(self, **kwargs: Any) -> torch.Tensor:
        """
        Samples diffusion process based on the provided configurations.

        Returns:
            torch.Tensor: The result of the diffusion sampling process.
        """
        _configs = {
            key: self.configs.sample_diffusion.get(key)
            for key in [
                "gamma0",
                "gamma_min",
                "noise_scale_lambda",
                "step_scale_eta",
            ]
        }
        _configs.update(
            {
                "attn_chunk_size": (
                    self.configs.infer_setting.chunk_size if not self.training else None
                ),
                "diffusion_chunk_size": (
                    self.configs.infer_setting.sample_diffusion_chunk_size
                    if not self.training
                    else None
                ),
            }
        )
        _configs.update(
            {
                "guidance_configs": self.configs.sample_diffusion.to_dict().get(
                    "guidance"
                )
            }
        )
        return autocasting_disable_decorator(self.configs.skip_amp.sample_diffusion)(
            sample_diffusion
        )(**_configs, **kwargs)

    def run_confidence_head(self, *args: Any, **kwargs: Any) -> Any:
        """
        Runs the confidence head with optional automatic mixed precision (AMP) disabled.

        Returns:
            Any: The output of the confidence head.
        """
        return autocasting_disable_decorator(self.configs.skip_amp.confidence_head)(
            self.confidence_head
        )(*args, **kwargs)

    def main_inference_loop(
        self,
        input_feature_dict: dict[str, Any],
        label_dict: dict[str, Any],
        N_cycle: int,
        mode: str,
        inplace_safe: bool = True,
        chunk_size: Optional[int] = 4,
        N_model_seed: int = 1,
        symmetric_permutation: SymmetricPermutation = None,
        mc_dropout_apply_rate: float = 0.4,
    ) -> tuple[dict[str, torch.Tensor], dict[str, Any], dict[str, Any]]:
        """
        Main inference loop (multiple model seeds) for the Alphafold3 model.

        Args:
            input_feature_dict (dict[str, Any]): Input features dictionary.
            label_dict (dict[str, Any]): Label dictionary.
            N_cycle (int): Number of cycles.
            mode (str): Mode of operation (e.g., 'inference').
            inplace_safe (bool): Whether to use inplace operations safely. Defaults to True.
            chunk_size (Optional[int]): Chunk size for memory-efficient operations. Defaults to 4.
            N_model_seed (int): Number of model seeds. Defaults to 1.
            symmetric_permutation (SymmetricPermutation): Symmetric permutation object. Defaults to None.
            mc_dropout_apply_rate (float): Only for inference mode

        Returns:
            tuple[dict[str, torch.Tensor], dict[str, Any], dict[str, Any]]: Prediction, log, and time dictionaries.
        """
        # For backward compatibility, if N_model_seed > 1, process multiple seeds here
        # But in evaluation mode, this should be handled externally
        if N_model_seed > 1 and mode in ["inference"]:
            pred_dicts = []
            log_dicts = []
            time_trackers = []
            for _ in range(N_model_seed):
                pred_dict, log_dict, time_tracker = self._main_inference_loop(
                    input_feature_dict=(
                        copy.deepcopy(input_feature_dict)
                        if (N_model_seed > 1 and mode == "inference")
                        else input_feature_dict
                    ),  # the input_feature_dict is modified when mode is "inference"
                    label_dict=label_dict,
                    N_cycle=N_cycle,
                    mode=mode,
                    inplace_safe=inplace_safe,
                    chunk_size=chunk_size,
                    symmetric_permutation=symmetric_permutation,
                    mc_dropout=random.random() < mc_dropout_apply_rate,
                )
                pred_dicts.append(pred_dict)
                log_dicts.append(log_dict)
                time_trackers.append(time_tracker)

            # Combine outputs of multiple models
            def _cat(dict_list, key):
                return torch.cat([x[key] for x in dict_list], dim=0)

            def _list_join(dict_list, key):
                return sum([x[key] for x in dict_list], [])

            all_pred_dict = {
                "coordinate": _cat(pred_dicts, "coordinate"),
                "summary_confidence": _list_join(pred_dicts, "summary_confidence"),
                "full_data": _list_join(pred_dicts, "full_data"),
                "plddt": _cat(pred_dicts, "plddt"),
                "pae": _cat(pred_dicts, "pae"),
                "pde": _cat(pred_dicts, "pde"),
                "resolved": _cat(pred_dicts, "resolved"),
            }

            all_log_dict = simple_merge_dict_list(log_dicts)
            all_time_dict = simple_merge_dict_list(time_trackers)
            return all_pred_dict, all_log_dict, all_time_dict
        else:
            # Single seed inference - delegate to _main_inference_loop
            return self._main_inference_loop(
                input_feature_dict=input_feature_dict,
                label_dict=label_dict,
                N_cycle=N_cycle,
                mode=mode,
                inplace_safe=inplace_safe,
                chunk_size=chunk_size,
                symmetric_permutation=symmetric_permutation,
                mc_dropout=random.random() < mc_dropout_apply_rate,
            )

    def _get_dynamic_chunk_size(self, N_token: int) -> Optional[int]:
        """
        Get dynamic chunk_size based on token count

        Args:
            N_token (int): Number of tokens

        Returns:
            Optional[int]: Optimal chunk_size for the given token count
        """
        if not hasattr(self.configs.infer_setting, "chunk_size_thresholds"):
            return self.configs.infer_setting.chunk_size

        thresholds = self.configs.infer_setting.chunk_size_thresholds

        # Convert string keys to integers and sort in ascending order
        threshold_pairs = [(int(k), v) for k, v in thresholds.items()]
        sorted_thresholds = sorted(threshold_pairs, key=lambda x: x[0])

        # Find the appropriate chunk_size for the given token count
        for threshold, chunk_size in sorted_thresholds:
            if N_token <= threshold:
                return None if chunk_size == -1 else chunk_size

        # For token counts larger than the largest threshold, use smallest chunk_size
        return 32  # extreme case for very large proteins

    def _main_inference_loop(
        self,
        input_feature_dict: dict[str, Any],
        label_dict: dict[str, Any],
        N_cycle: int,
        mode: str,
        inplace_safe: bool = True,
        chunk_size: Optional[int] = 4,
        symmetric_permutation: SymmetricPermutation = None,
        mc_dropout: bool = False,
    ) -> tuple[dict[str, torch.Tensor], dict[str, Any], dict[str, Any]]:
        """
        Main inference loop (single model seed) for the Alphafold3 model.
        mc_dropout: do not use by default

        Returns:
            tuple[dict[str, torch.Tensor], dict[str, Any], dict[str, Any]]: Prediction, log, and time dictionaries.
        """
        step_st = time.time()
        N_token = input_feature_dict["residue_index"].shape[-1]

        # Apply dynamic chunk_size if enabled (otherwise keep the passed chunk_size)
        if (
            hasattr(self.configs.infer_setting, "dynamic_chunk_size")
            and self.configs.infer_setting.dynamic_chunk_size
        ):
            chunk_size = self._get_dynamic_chunk_size(N_token)
        # If dynamic chunking is disabled, chunk_size keeps its original value from the function parameter

        log_dict = {}
        pred_dict = {}
        time_tracker = {}

        s_inputs, s, z = self.get_pairformer_output(
            input_feature_dict=input_feature_dict,
            N_cycle=N_cycle,
            inplace_safe=inplace_safe,
            chunk_size=chunk_size,
            mc_dropout=mc_dropout,
        )

        keys_to_delete = []
        for key in input_feature_dict.keys():
            if "template_" in key or key in [
                "msa",
                "has_deletion",
                "deletion_value",
                "profile",
                "deletion_mean",
                "bond_mask",  # only the training loss reads this; ~2.4G dead weight at inference
                # "token_bonds",
            ]:
                keys_to_delete.append(key)

        for key in keys_to_delete:
            del input_feature_dict[key]
        step_trunk = time.time()
        time_tracker.update({"pairformer": step_trunk - step_st})
        # Sample diffusion
        # [..., N_sample, N_atom, 3]
        N_sample = self.configs.sample_diffusion["N_sample"]
        N_step = self.configs.sample_diffusion["N_step"]

        noise_schedule = self.inference_noise_scheduler(
            N_step=N_step, device=s_inputs.device, dtype=s_inputs.dtype
        )
        cache = dict()
        low_vram = os.environ.get("PROTENIX_LOW_VRAM", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if low_vram and torch.cuda.is_available():
            # The diffusion stage is the OOM frontier once the trunk fits. Log what is
            # resident before conditioning so a future OOM here (or in the transformer
            # after it) names the big tensors directly instead of guessing.
            _gib = lambda t: t.nelement() * t.element_size() / 1e9
            feat_top = sorted(
                ((k, _gib(v)) for k, v in input_feature_dict.items() if torch.is_tensor(v)),
                key=lambda kv: -kv[1],
            )[:4]
            logger.info(
                f"[mem] pre-diffusion: alloc={torch.cuda.memory_allocated() / 1e9:.2f}G "
                f"peak={torch.cuda.max_memory_allocated() / 1e9:.2f}G | "
                f"z={tuple(z.shape)}{z.dtype} "
                f"relp={tuple(input_feature_dict['relp'].shape)}"
                f"{input_feature_dict['relp'].dtype} s={tuple(s.shape)} | "
                f"top_feat=[" + ", ".join(f"{k}:{b:.2f}G" for k, b in feat_top) + "]"
            )
        # Row-chunk the pair conditioning under low-VRAM: pair_z is built in fp32
        # (autocasting disabled) and the cat/LayerNorm/transition region peaks at
        # several [chunk, N, 2*c_z] fp32 tensors. With c_z=256 a 512-row chunk is
        # already ~4.5G, so chunk small (64 rows) to keep the per-chunk peak <1G.
        # chunk_layer still pre-allocates the full pair_z output -- that is unavoidable.
        pair_chunk = 64 if low_vram else None
        if self.enable_diffusion_shared_vars_cache:
            # line 1-5 of algorithm 21 calculate z in diffusion conditioning
            cache["pair_z"] = autocasting_disable_decorator(
                self.configs.skip_amp.sample_diffusion
            )(self.diffusion_module.diffusion_conditioning.prepare_cache)(
                input_feature_dict["relp"], z, False, chunk_size=pair_chunk
            )
            # Keep pair_z in bf16 under low-VRAM: prepare_cache's Linears are precision=fp32
            # so the output is fp32 (~8.8 GiB at c_z=256). Casting it to bf16 halves the cache
            # and the per-step clone DiffusionConditioning.forward takes (17.6 -> 8.8 GiB
            # across the 200-step denoise loop). f_forward skips its fp32 upcasts under
            # low-VRAM, so this bf16 propagates into the transformer instead of being doubled
            # back to fp32 each step.
            if low_vram:
                cache["pair_z"] = cache["pair_z"].to(torch.bfloat16)
            # z is not consumed during diffusion (sample_diffusion gets z_trunk=None below
            # because pair_z is cached), but the distogram/confidence heads after diffusion
            # still read it. Park it on the host for the 200-step denoise loop and restore it
            # before the heads -- frees the ~2.4 GiB pair tensor for diffusion (the margin it
            # needs to fit) for a single cheap H2D copy, instead of dropping it for good.
            if low_vram:
                z = z.cpu()
            if low_vram and torch.cuda.is_available():
                torch.cuda.empty_cache()
                pz = cache["pair_z"]
                logger.info(
                    f"[mem] post-pair_z: alloc={torch.cuda.memory_allocated() / 1e9:.2f}G "
                    f"peak={torch.cuda.max_memory_allocated() / 1e9:.2f}G | "
                    f"pair_z={tuple(pz.shape)}{pz.dtype} "
                    f"({pz.nelement() * pz.element_size() / 1e9:.2f}G)"
                )
            cache["p_lm/c_l"] = autocasting_disable_decorator(
                self.configs.skip_amp.sample_diffusion
            )(self.diffusion_module.atom_attention_encoder.prepare_cache)(
                ref_pos=input_feature_dict["ref_pos"],
                ref_charge=input_feature_dict["ref_charge"],
                ref_mask=input_feature_dict["ref_mask"],
                ref_element=input_feature_dict["ref_element"],
                ref_atom_name_chars=input_feature_dict["ref_atom_name_chars"],
                atom_to_token_idx=input_feature_dict["atom_to_token_idx"],
                d_lm=input_feature_dict["d_lm"],
                v_lm=input_feature_dict["v_lm"],
                pad_info=input_feature_dict["pad_info"],
                r_l=True,
                z=cache["pair_z"],
                inplace_safe=False,
            )
        else:
            cache["pair_z"] = None
            cache["p_lm/c_l"] = [None, None]
        p2d = _load_p2d_side_channels()
        p2d_coords = None
        if "coords" in p2d:
            ref = torch.from_numpy(p2d["coords"])
            ref_mask = torch.from_numpy(p2d["mask"])
            n_atom_ref = input_feature_dict["atom_to_token_idx"].size(-1)
            if (
                ref.dim() == 2
                and ref.shape[0] == n_atom_ref
            ) or (
                ref.dim() == 3
                and ref.shape[-2:] == (n_atom_ref, 3)
            ):
                p2d_coords = ref.to(s_inputs.device)
                p2d_coord_mask = ref_mask.to(s_inputs.device)
            else:
                logger.warning(
                    f"protenix2dock init coords N={ref.shape[0]} does not match "
                    f"assembled N_atom={input_feature_dict['atom_to_token_idx'].size(-1)}; "
                    "falling back to noise initialisation."
                )
        if "contacts" in p2d and "pairwise_distance_index" in input_feature_dict:
            contacts = p2d["contacts"]
            idx = torch.from_numpy(contacts["index"]).to(s_inputs.device)
            upper = torch.from_numpy(contacts["upper"]).to(s_inputs.device)
            lower = torch.zeros_like(upper)
            existing = input_feature_dict["pairwise_distance_index"]
            input_feature_dict["pairwise_distance_index"] = torch.cat(
                [existing, idx.T], dim=-1
            )
            input_feature_dict["pairwise_distance_upper_bound"] = torch.cat(
                [
                    input_feature_dict["pairwise_distance_upper_bound"],
                    upper,
                ],
                dim=-1,
            )
            input_feature_dict["pairwise_distance_lower_bound"] = torch.cat(
                [
                    input_feature_dict["pairwise_distance_lower_bound"],
                    lower,
                ],
                dim=-1,
            )
            input_feature_dict["pairwise_distance_is_bond"] = torch.cat(
                [
                    input_feature_dict["pairwise_distance_is_bond"],
                    torch.zeros_like(upper),
                ],
                dim=-1,
            )
            # Mark contact pairs as the angle category: clash pairs lose their
            # upper bound (upper=inf) inside PairwiseDistancePotential, while
            # angle-category pairs keep a finite upper bound -- exactly the
            # pocket-anchoring semantics protenix2dock needs.
            input_feature_dict["pairwise_distance_is_angle"] = torch.cat(
                [
                    input_feature_dict["pairwise_distance_is_angle"],
                    torch.ones_like(upper),
                ],
                dim=-1,
            )
            logger.info(
                f"protenix2dock: injected {idx.shape[0]} pocket contact pairs into TFG."
            )
        # Free-chain covalent bonds also enter the official TFG soft channel
        # (is_bond=1): PairwiseDistancePotential applies its bond buffer,
        # VDW clamps and angles-then-bonds projection ordering to the x0
        # prediction of every guided step, keeping the denoiser's
        # clean-structure estimate chemically valid.
        if (
            p2d.get("cov_index") is not None
            and "pairwise_distance_index" in input_feature_dict
        ):
            cov_idx = _p2d_tensor(p2d, "cov_index").to(s_inputs.device)
            cov_up = _p2d_tensor(p2d, "cov_upper").to(s_inputs.device)
            cov_lo = _p2d_tensor(p2d, "cov_lower").to(s_inputs.device)
            input_feature_dict["pairwise_distance_index"] = torch.cat(
                [input_feature_dict["pairwise_distance_index"], cov_idx], dim=-1
            )
            input_feature_dict["pairwise_distance_upper_bound"] = torch.cat(
                [input_feature_dict["pairwise_distance_upper_bound"], cov_up],
                dim=-1,
            )
            input_feature_dict["pairwise_distance_lower_bound"] = torch.cat(
                [input_feature_dict["pairwise_distance_lower_bound"], cov_lo],
                dim=-1,
            )
            input_feature_dict["pairwise_distance_is_bond"] = torch.cat(
                [
                    input_feature_dict["pairwise_distance_is_bond"],
                    torch.ones_like(cov_up),
                ],
                dim=-1,
            )
            input_feature_dict["pairwise_distance_is_angle"] = torch.cat(
                [
                    input_feature_dict["pairwise_distance_is_angle"],
                    torch.zeros_like(cov_up),
                ],
                dim=-1,
            )
            logger.info(
                f"protenix2dock: injected {cov_idx.shape[1]} free-chain covalent "
                "bond pairs into TFG (is_bond)."
            )
        if p2d.get("score_only") and p2d_coords is not None:
            # Score mode: skip diffusion entirely and evaluate the confidence
            # heads directly on the input coordinates. Ensembles carry no
            # meaning without diffusion — score the first start.
            p2d_score = (p2d_coords[0] if p2d_coords.dim() == 3 else p2d_coords)
            n_atom = p2d_score.shape[0]
            # float32: the dumper converts coordinates to numpy, which has no
            # bf16 dtype (the trunk runs bf16 under PROTENIX_LOW_VRAM).
            pred_dict["coordinate"] = (
                p2d_score.view(1, n_atom, 3)
                .expand(N_sample, n_atom, 3)
                .contiguous()
                .float()
            )
            logger.info("protenix2dock: score-only mode, diffusion bypassed.")
        else:
            sample_kwargs = dict(
                denoise_net=self.diffusion_module,
                input_feature_dict=input_feature_dict,
                s_inputs=s_inputs,
                s_trunk=s,
                z_trunk=None if cache["pair_z"] is not None else z,
                pair_z=cache["pair_z"],
                p_lm=cache["p_lm/c_l"][0],
                c_l=cache["p_lm/c_l"][1],
                N_sample=N_sample,
                noise_schedule=noise_schedule,
                inplace_safe=inplace_safe,
                enable_efficient_fusion=self.enable_efficient_fusion,
                init_coords=p2d_coords,
                init_mask=(
                    p2d_coord_mask
                    if p2d_coords is not None
                    else None
                ),
                init_noise_scale=float(p2d.get("noise_scale", 0.0)),
                pin_mask=(
                    torch.from_numpy(p2d["pin"]).to(s_inputs.device)
                    if p2d_coords is not None and "pin" in p2d
                    else None
                ),
                anchor_index=(
                    _p2d_tensor(p2d, "anchor_index").to(s_inputs.device)
                    if p2d.get("anchor_index") is not None
                    else None
                ),
                anchor_upper=(
                    _p2d_tensor(p2d, "anchor_upper").to(s_inputs.device)
                    if p2d.get("anchor_upper") is not None
                    else None
                ),
                anchor_lower=(
                    _p2d_tensor(p2d, "anchor_lower").to(s_inputs.device)
                    if p2d.get("anchor_lower") is not None
                    else None
                ),
                # Free-chain covalent bonds as their OWN projection family:
                # the generator projects them AFTER the pocket/clash bands
                # each step (official angle->bond ordering in
                # tfg.potentials.PairwiseDistancePotential._project) so bond
                # geometry wins the negotiation. Independent of the anchor
                # npz — blind/no-guidance runs keep them (chemistry, not
                # guidance).
                bond_index=(
                    _p2d_tensor(p2d, "cov_index").to(s_inputs.device)
                    if p2d.get("cov_index") is not None
                    else None
                ),
                bond_upper=(
                    _p2d_tensor(p2d, "cov_upper").to(s_inputs.device)
                    if p2d.get("cov_upper") is not None
                    else None
                ),
                bond_lower=(
                    _p2d_tensor(p2d, "cov_lower").to(s_inputs.device)
                    if p2d.get("cov_lower") is not None
                    else None
                ),
            )
            if p2d_coords is not None:
                quads, signs = _build_chiral_quads(
                    input_feature_dict, p2d_coords, p2d.get("pin")
                )
                if quads is not None:
                    sample_kwargs["chiral_quads"] = quads.to(s_inputs.device)
                    sample_kwargs["chiral_sign"] = signs.to(s_inputs.device)
            pred_dict["coordinate"] = self.sample_diffusion(**sample_kwargs)

        step_diffusion = time.time()
        time_tracker.update({"diffusion": step_diffusion - step_trunk})
        # Bring the trunk pair tensor back to the GPU for the distogram/confidence heads
        # (parked on the host during diffusion under low-VRAM above).
        if z.device.type == "cpu":
            z = z.to(s_inputs.device)
        # Distogram logits: log contact_probs only, to reduce the dimension
        pred_dict["contact_probs"] = autocasting_disable_decorator(True)(
            sample_confidence.compute_contact_prob
        )(
            distogram_logits=self.distogram_head(z),
            **sample_confidence.get_bin_params(self.configs.loss.distogram),
        )  # [N_token, N_token]

        # Confidence logits
        (
            pred_dict["plddt"],
            pred_dict["pae"],
            pred_dict["pde"],
            pred_dict["resolved"],
        ) = self.run_confidence_head(
            input_feature_dict=input_feature_dict,
            s_inputs=s_inputs,
            s_trunk=s,
            z_trunk=z,
            pair_mask=None,
            x_pred_coords=pred_dict["coordinate"],
            triangle_multiplicative=self.configs.triangle_multiplicative,
            triangle_attention=self.configs.triangle_attention,
            inplace_safe=inplace_safe,
            chunk_size=chunk_size,
        )

        step_confidence = time.time()
        time_tracker.update({"confidence": step_confidence - step_diffusion})
        time_tracker.update({"model_forward": time.time() - step_st})

        # Permutation: when label is given, permute coordinates and other heads
        if label_dict is not None and symmetric_permutation is not None:
            pred_dict, log_dict = symmetric_permutation.permute_inference_pred_dict(
                input_feature_dict=input_feature_dict,
                pred_dict=pred_dict,
                label_dict=label_dict,
                permute_by_pocket=("pocket_mask" in label_dict)
                and ("interested_ligand_mask" in label_dict),
            )
            last_step_seconds = step_confidence
            time_tracker.update({"permutation": time.time() - last_step_seconds})

        # Summary Confidence & Full Data
        # Computed after coordinates and logits are permuted
        if label_dict is None:
            interested_atom_mask = None
        else:
            interested_atom_mask = label_dict.get("interested_ligand_mask", None)
        (
            pred_dict["summary_confidence"],
            pred_dict["full_data"],
        ) = autocasting_disable_decorator(True)(
            sample_confidence.compute_full_data_and_summary
        )(
            configs=self.configs,
            pae_logits=pred_dict["pae"],
            plddt_logits=pred_dict["plddt"],
            pde_logits=pred_dict["pde"],
            contact_probs=pred_dict.get(
                "per_sample_contact_probs", pred_dict["contact_probs"]
            ),
            token_asym_id=input_feature_dict["asym_id"],
            token_has_frame=input_feature_dict["has_frame"],
            atom_coordinate=pred_dict["coordinate"],
            atom_to_token_idx=input_feature_dict["atom_to_token_idx"],
            atom_is_polymer=1 - input_feature_dict["is_ligand"],
            N_recycle=N_cycle,
            interested_atom_mask=interested_atom_mask,
            return_full_data=True,
            mol_id=(input_feature_dict["mol_id"] if mode != "inference" else None),
            elements_one_hot=(
                input_feature_dict["ref_element"] if mode != "inference" else None
            ),
        )

        # protenix2dock native affinity: read out directly from the trunk
        # representations and (predicted or input) coordinates — same fusion
        # point as the confidence head — and merge into summary_confidence so
        # the stock dumper writes it with zero layout changes.
        if getattr(self, "affinity_heads", None) is None and getattr(self, "_affinity_ckpt_path", ""):
            # Supports comma-separated checkpoints: all heads run and their
            # value predictions are averaged (ensemble), with cross-head
            # spread surfaced as affinity_pred_std.
            from protenix.model.modules.affinity import ProtenixAffinityHead

            heads = []
            for ckpt_path in [p.strip() for p in self._affinity_ckpt_path.split(",") if p.strip()]:
                if not os.path.exists(ckpt_path):
                    logger.warning(f"protenix2dock affinity ckpt missing: {ckpt_path}")
                    continue
                blob = torch.load(ckpt_path, map_location="cpu", weights_only=False)
                head_cfg = dict(blob.get("config") or {})
                head = ProtenixAffinityHead(**head_cfg)
                head.load_state_dict(blob["state_dict"])
                head.to(z.device)
                head.eval()
                heads.append(head)
            self.affinity_heads = heads
            if heads:
                logger.info(
                    f"protenix2dock affinity: {len(heads)} head(s) loaded "
                    f"({sum(p.numel() for p in heads[0].parameters())/1e6:.1f}M params each)."
                )
        if getattr(self, "affinity_heads", None):
            try:
                all_entries = []
                for head in self.affinity_heads:
                    all_entries.append(head(
                        s_inputs=s_inputs,
                        z_trunk=z,
                        x_pred=pred_dict["coordinate"],
                        atom_to_token_idx=input_feature_dict["atom_to_token_idx"],
                        atom_is_ligand=input_feature_dict["is_ligand"],
                    ))
                # Ensemble across heads: mean value, cross-head std.
                aff_entries = []
                for s_i in range(len(all_entries[0])):
                    merged = dict(all_entries[0][s_i])
                    if len(all_entries) > 1:
                        vals = [e[s_i].get("affinity_pred_value") for e in all_entries
                                if e[s_i].get("affinity_pred_value") is not None]
                        merged["affinity_pred_value"] = sum(vals) / len(vals)
                        if len(vals) > 1:
                            merged["affinity_pred_std"] = (
                                sum((v - merged["affinity_pred_value"]) ** 2 for v in vals)
                                / len(vals)
                            ) ** 0.5
                    aff_entries.append(merged)
                for entry, summary in zip(aff_entries, pred_dict["summary_confidence"]):
                    if entry:
                        summary.update(entry)
                logger.info(
                    "protenix2dock affinity: "
                    + "; ".join(
                        f"sample{i}={e.get('affinity_pred_value', float('nan')):.3f}"
                        for i, e in enumerate(aff_entries)
                        if e
                    )
                )
            except Exception as exc:
                import traceback

                logger.warning(
                    f"protenix2dock affinity head failed: {exc}\n"
                    + traceback.format_exc()
                )

        return pred_dict, log_dict, time_tracker

    def main_train_loop(
        self,
        input_feature_dict: dict[str, Any],
        label_full_dict: dict[str, Any],
        label_dict: dict[str, Any],
        N_cycle: int,
        symmetric_permutation: SymmetricPermutation,
        inplace_safe: bool = False,
        chunk_size: Optional[int] = None,
    ) -> tuple[dict[str, torch.Tensor], dict[str, Any], dict[str, Any]]:
        """
        Main training loop for the Alphafold3 model.

        Args:
            input_feature_dict (dict[str, Any]): Input features dictionary.
            label_full_dict (dict[str, Any]): Full label dictionary (uncropped).
            label_dict (dict): Label dictionary (cropped).
            N_cycle (int): Number of cycles.
            symmetric_permutation (SymmetricPermutation): Symmetric permutation object.
            inplace_safe (bool): Whether to use inplace operations safely. Defaults to False.
            chunk_size (Optional[int]): Chunk size for memory-efficient operations. Defaults to None.

        Returns:
            tuple[dict[str, torch.Tensor], dict[str, Any], dict[str, Any]]:
                Prediction, updated label, and log dictionaries.
        """

        s_inputs, s, z = self.get_pairformer_output(
            input_feature_dict=input_feature_dict,
            N_cycle=N_cycle,
            inplace_safe=inplace_safe,
            chunk_size=chunk_size,
        )

        log_dict = {}
        pred_dict = {}

        cache = dict()
        if self.enable_diffusion_shared_vars_cache:
            cache["pair_z"] = autocasting_disable_decorator(
                self.configs.skip_amp.sample_diffusion
            )(self.diffusion_module.diffusion_conditioning.prepare_cache)(
                input_feature_dict["relp"], z, False
            )
            cache["p_lm/c_l"] = autocasting_disable_decorator(
                self.configs.skip_amp.sample_diffusion
            )(self.diffusion_module.atom_attention_encoder.prepare_cache)(
                ref_pos=input_feature_dict["ref_pos"],
                ref_charge=input_feature_dict["ref_charge"],
                ref_mask=input_feature_dict["ref_mask"],
                ref_element=input_feature_dict["ref_element"],
                ref_atom_name_chars=input_feature_dict["ref_atom_name_chars"],
                atom_to_token_idx=input_feature_dict["atom_to_token_idx"],
                d_lm=input_feature_dict["d_lm"],
                v_lm=input_feature_dict["v_lm"],
                pad_info=input_feature_dict["pad_info"],
                r_l=True,
                z=cache["pair_z"],
                inplace_safe=False,
            )
        else:
            cache["pair_z"] = None
            cache["p_lm/c_l"] = [None, None]
        # Mini-rollout: used for confidence and label permutation
        with torch.no_grad():
            # [..., 1, N_atom, 3]
            N_sample_mini_rollout = self.configs.sample_diffusion[
                "N_sample_mini_rollout"
            ]  # =1
            N_step_mini_rollout = self.configs.sample_diffusion["N_step_mini_rollout"]
            self.diffusion_module.eval()  # use eval mode for mini-rollout
            coordinate_mini = self.sample_diffusion(
                denoise_net=self.diffusion_module,
                input_feature_dict=input_feature_dict,
                s_inputs=s_inputs.detach(),
                s_trunk=s.detach(),
                z_trunk=None if cache["pair_z"] is not None else z.detach(),
                pair_z=None if cache["pair_z"] is None else cache["pair_z"].detach(),
                p_lm=(
                    None
                    if cache["p_lm/c_l"][0] is None
                    else cache["p_lm/c_l"][0].detach()
                ),
                c_l=(
                    None
                    if cache["p_lm/c_l"][1] is None
                    else cache["p_lm/c_l"][1].detach()
                ),
                N_sample=N_sample_mini_rollout,
                noise_schedule=self.inference_noise_scheduler(
                    N_step=N_step_mini_rollout,
                    device=s_inputs.device,
                    dtype=s_inputs.dtype,
                ),
                enable_efficient_fusion=self.enable_efficient_fusion,
            )
            self.diffusion_module.train()
            coordinate_mini.detach_()
            pred_dict["coordinate_mini"] = coordinate_mini

            # Permute ground truth to match mini-rollout prediction
            (
                label_dict,
                perm_log_dict,
            ) = symmetric_permutation.permute_label_to_match_mini_rollout(
                coordinate_mini,
                input_feature_dict,
                label_dict,
                label_full_dict,
            )
            log_dict.update(perm_log_dict)

        # Confidence: use mini-rollout prediction, and detach token embeddings
        drop_embedding = (
            random.random() < self.configs.model.confidence_embedding_drop_rate
        )
        plddt_pred, pae_pred, pde_pred, resolved_pred = self.run_confidence_head(
            input_feature_dict=input_feature_dict,
            s_inputs=s_inputs,
            s_trunk=s,
            z_trunk=z,
            pair_mask=None,
            x_pred_coords=coordinate_mini,
            use_embedding=not drop_embedding,
            triangle_multiplicative=self.configs.triangle_multiplicative,
            triangle_attention=self.configs.triangle_attention,
            inplace_safe=inplace_safe,
            chunk_size=chunk_size,
        )
        pred_dict.update(
            {
                "plddt": plddt_pred,
                "pae": pae_pred,
                "pde": pde_pred,
                "resolved": resolved_pred,
            }
        )

        if self.train_confidence_only:
            # Skip diffusion loss and distogram loss. Return now.
            return pred_dict, label_dict, log_dict

        # Denoising: use permuted coords to generate noisy samples and perform denoising
        # x_denoised: [..., N_sample, N_atom, 3]
        # x_noise_level: [..., N_sample]
        N_sample = self.diffusion_batch_size
        drop_conditioning = (
            random.random() < self.configs.model.condition_embedding_drop_rate
        )
        _, x_denoised, x_noise_level = autocasting_disable_decorator(
            self.configs.skip_amp.sample_diffusion_training
        )(sample_diffusion_training)(
            noise_sampler=self.train_noise_sampler,
            denoise_net=self.diffusion_module,
            label_dict=label_dict,
            input_feature_dict=input_feature_dict,
            s_inputs=s_inputs,
            s_trunk=s,
            z_trunk=None if cache["pair_z"] is not None else z,
            pair_z=cache["pair_z"],
            p_lm=cache["p_lm/c_l"][0],
            c_l=cache["p_lm/c_l"][1],
            N_sample=N_sample,
            diffusion_chunk_size=self.configs.diffusion_chunk_size,
            use_conditioning=not drop_conditioning,
            enable_efficient_fusion=self.enable_efficient_fusion,
        )
        pred_dict.update(
            {
                "distogram": autocasting_disable_decorator(True)(self.distogram_head)(
                    z
                ),
                # [..., N_sample=48, N_atom, 3]: diffusion loss
                "coordinate": x_denoised,
                "noise_level": x_noise_level,
            }
        )

        # Permute symmetric atom/chain in each sample to match true structure
        # Note: currently chains cannot be permuted since label is cropped
        (
            pred_dict,
            perm_log_dict,
            _,
            _,
        ) = symmetric_permutation.permute_diffusion_sample_to_match_label(
            input_feature_dict, pred_dict, label_dict, stage="train"
        )
        log_dict.update(perm_log_dict)
        log_dict.update({"noise_level": x_noise_level})

        return pred_dict, label_dict, log_dict

    def forward(
        self,
        input_feature_dict: dict[str, Any],
        label_full_dict: dict[str, Any],
        label_dict: dict[str, Any],
        mode: str = "inference",
        current_step: Optional[int] = None,
        symmetric_permutation: SymmetricPermutation = None,
        disable_inplace: bool = False,
        mc_dropout_apply_rate: float = 0.4,
    ) -> tuple[dict[str, torch.Tensor], dict[str, Any], dict[str, Any]]:
        """
        Forward pass of the Alphafold3 model.

        Args:
            input_feature_dict (dict[str, Any]): Input features dictionary.
            label_full_dict (dict[str, Any]): Full label dictionary (uncropped).
            label_dict (dict[str, Any]): Label dictionary (cropped).
            mode (str): Mode of operation ('train', 'inference', 'eval'). Defaults to 'inference'.
            current_step (Optional[int]): Current training step. Defaults to None.
            symmetric_permutation (SymmetricPermutation): Symmetric permutation object. Defaults to None.

        Returns:
            tuple[dict[str, torch.Tensor], dict[str, Any], dict[str, Any]]:
                Prediction, updated label, and log dictionaries.
        """

        assert mode in ["train", "eval", "inference"]
        not_use_gradient = not (self.training or torch.is_grad_enabled())
        inplace_safe = not_use_gradient and (not disable_inplace)

        input_feature_dict = self.relative_position_encoding.generate_relp(
            input_feature_dict
        )
        input_feature_dict = update_input_feature_dict(input_feature_dict)

        if mode == "train":
            nc_rng = np.random.RandomState(current_step)
            N_cycle = nc_rng.randint(1, self.N_cycle + 1)
            assert self.training
            assert label_dict is not None
            assert symmetric_permutation is not None

            pred_dict, label_dict, log_dict = self.main_train_loop(
                input_feature_dict=input_feature_dict,
                label_full_dict=label_full_dict,
                label_dict=label_dict,
                N_cycle=N_cycle,
                symmetric_permutation=symmetric_permutation,
                inplace_safe=inplace_safe,
                chunk_size=None,
            )
            log_dict["N_cycle"] = N_cycle
        elif mode == "inference":
            pred_dict, log_dict, time_tracker = self.main_inference_loop(
                input_feature_dict=input_feature_dict,
                label_dict=None,
                N_cycle=self.N_cycle,
                mode=mode,
                inplace_safe=inplace_safe,
                chunk_size=self.configs.infer_setting.chunk_size,
                N_model_seed=self.N_model_seed,
                symmetric_permutation=None,
                mc_dropout_apply_rate=mc_dropout_apply_rate,
            )
            log_dict.update({"time": time_tracker})
        elif mode == "eval":
            if label_dict is not None:
                assert (
                    label_dict["coordinate"].size()
                    == label_full_dict["coordinate"].size()
                )
                label_dict.update(label_full_dict)

            pred_dict, log_dict, time_tracker = self.main_inference_loop(
                input_feature_dict=input_feature_dict,
                label_dict=label_dict,
                N_cycle=self.N_cycle,
                mode=mode,
                inplace_safe=inplace_safe,
                chunk_size=self.configs.infer_setting.chunk_size,
                N_model_seed=1,
                symmetric_permutation=symmetric_permutation,
                mc_dropout_apply_rate=mc_dropout_apply_rate,
            )
            log_dict.update({"time": time_tracker})

        return pred_dict, label_dict, log_dict
