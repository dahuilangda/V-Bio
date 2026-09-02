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

import os
from typing import Any, Callable, Optional

import torch

from protenix.model.utils import centre_random_augmentation
from protenix.tfg import parse_tfg_config, TFGEngine
from protenix.utils.logger import get_logger

logger = get_logger(__name__)


class TrainingNoiseSampler:
    """
    Sample the noise-level of training samples.

    Args:
        p_mean (float, optional): gaussian mean. Defaults to -1.2.
        p_std (float, optional): gaussian std. Defaults to 1.5.
        sigma_data (float, optional): scale. Defaults to 16.0, but this is 1.0 in EDM.
    """

    def __init__(
        self,
        p_mean: float = -1.2,
        p_std: float = 1.5,
        sigma_data: float = 16.0,  # NOTE: in EDM, this is 1.0
    ) -> None:
        self.sigma_data = sigma_data
        self.p_mean = p_mean
        self.p_std = p_std
        print(f"train scheduler {self.sigma_data}")

    def __call__(
        self, size: torch.Size, device: torch.device = torch.device("cpu")
    ) -> torch.Tensor:
        """Sampling

        Args:
            size (torch.Size): the target size
            device (torch.device, optional): target device. Defaults to torch.device("cpu").

        Returns:
            torch.Tensor: sampled noise-level
        """
        rnd_normal = torch.randn(size=size, device=device)
        noise_level = (rnd_normal * self.p_std + self.p_mean).exp() * self.sigma_data
        return noise_level


class InferenceNoiseScheduler:
    """
    Scheduler for noise-level (time steps).

    Args:
        s_max (float, optional): maximal noise level. Defaults to 160.0.
        s_min (float, optional): minimal noise level. Defaults to 4e-4.
        rho (float, optional): the exponent numerical part. Defaults to 7.
        sigma_data (float, optional): scale. Defaults to 16.0, but this is 1.0 in EDM.
    """

    def __init__(
        self,
        s_max: float = 160.0,
        s_min: float = 4e-4,
        rho: float = 7,
        sigma_data: float = 16.0,  # NOTE: in EDM, this is 1.0
    ) -> None:
        self.sigma_data = sigma_data
        self.s_max = s_max
        self.s_min = s_min
        self.rho = rho
        print(f"inference scheduler {self.sigma_data}")

    def __call__(
        self,
        N_step: int = 200,
        device: torch.device = torch.device("cpu"),
        dtype: torch.dtype = torch.float32,
    ) -> torch.Tensor:
        """Schedule the noise-level (time steps). No sampling is performed.

        Args:
            N_step (int, optional): number of time steps. Defaults to 200.
            device (torch.device, optional): target device. Defaults to torch.device("cpu").
            dtype (torch.dtype, optional): target dtype. Defaults to torch.float32.

        Returns:
            torch.Tensor: noise-level (time_steps)
                [N_step+1]
        """
        step_size = 1 / N_step
        step_indices = torch.arange(N_step + 1, device=device, dtype=dtype)
        t_step_list = (
            self.sigma_data
            * (
                self.s_max ** (1 / self.rho)
                + step_indices
                * step_size
                * (self.s_min ** (1 / self.rho) - self.s_max ** (1 / self.rho))
            )
            ** self.rho
        )
        # replace the last time step by 0
        t_step_list[..., -1] = 0  # t_N = 0

        return t_step_list


def sample_diffusion(
    denoise_net: Callable,
    input_feature_dict: dict[str, Any],
    s_inputs: torch.Tensor,
    s_trunk: torch.Tensor,
    z_trunk: torch.Tensor,
    pair_z: torch.Tensor,
    p_lm: torch.Tensor,
    c_l: torch.Tensor,
    noise_schedule: torch.Tensor,
    N_sample: int = 1,
    gamma0: float = 0.8,
    gamma_min: float = 1.0,
    noise_scale_lambda: float = 1.003,
    step_scale_eta: float = 1.5,
    diffusion_chunk_size: Optional[int] = None,
    inplace_safe: bool = False,
    attn_chunk_size: Optional[int] = None,
    enable_efficient_fusion: bool = False,
    guidance_configs: Optional[dict[str, Any]] = None,
    init_coords: Optional[torch.Tensor] = None,
    init_mask: Optional[torch.Tensor] = None,
    init_noise_scale: float = 0.0,
    pin_mask: Optional[torch.Tensor] = None,
    anchor_index: Optional[torch.Tensor] = None,
    anchor_upper: Optional[torch.Tensor] = None,
    anchor_lower: Optional[torch.Tensor] = None,
    chiral_quads: Optional[torch.Tensor] = None,
    chiral_sign: Optional[torch.Tensor] = None,
) -> torch.Tensor:
    """Implements Algorithm 18 in AF3.
    It performances denoising steps from time 0 to time T.
    The time steps (=noise levels) are given by noise_schedule.

    Args:
        denoise_net (Callable): the network that performs the denoising step.
        input_feature_dict (dict[str, Any]): input meta feature dict
        s_inputs (torch.Tensor): single embedding from InputFeatureEmbedder
            [..., N_tokens, c_s_inputs]
        s_trunk (torch.Tensor): single feature embedding from PairFormer (Alg17)
            [..., N_tokens, c_s]
        z_trunk (torch.Tensor): pair feature embedding from PairFormer (Alg17)
            [..., N_tokens, N_tokens, c_z]
        pair_z (torch.Tensor): pair feature embedding from InputFeatureEmbedder
            [..., N_tokens, N_tokens, c_z_inputs]
        p_lm (torch.Tensor): MSA embedding
            [..., N_tokens, c_p_lm]
        c_l (torch.Tensor): ligand embedding
            [..., N_tokens, c_c_l]
        noise_schedule (torch.Tensor): noise-level schedule (which is also the time steps) since sigma=t.
            [N_iterations]
        N_sample (int): number of generated samples
        gamma0 (float): params in Alg.18.
        gamma_min (float): params in Alg.18.
        noise_scale_lambda (float): params in Alg.18.
        step_scale_eta (float): params in Alg.18.
        diffusion_chunk_size (Optional[int]): Chunk size for diffusion operation. Defaults to None.
        inplace_safe (bool): Whether to inplace operations safely. Defaults to False.
        attn_chunk_size (Optional[int]): Chunk size for attention. Defaults to None.
        enable_efficient_fusion (bool): Whether to enable efficient fusion. Defaults to False.
        guidance_configs (Optional[dict[str, Any]]): training free guidance configs. Defaults to None.
        init_coords (Optional[torch.Tensor]): reference coordinates for pose/refine
            initialisation, aligned to the assembled atom order. [N_atom, 3].
            Atoms whose init_mask is 0 keep the standard Gaussian noise start.
        init_mask (Optional[torch.Tensor]): per-atom flag (1 = start from
            init_coords, 0 = start from noise). [N_atom].
        init_noise_scale (float): fraction of the schedule's initial noise level
            mixed into the initialised coordinates (0.0 = pure init).
        pin_mask (Optional[torch.Tensor]): per-atom flag for true inpainting
            (1 = clamped to init_coords after every step, 0 = denoised). The
            pinned atoms therefore stay bit-exact through the loop; use this
            for receptor-fixed peptide design/refinement.
        anchor_index (Optional[torch.Tensor]): [2, M] hard-anchor atom pairs.
        anchor_upper (Optional[torch.Tensor]): [M] upper bounds (A).
        anchor_lower (Optional[torch.Tensor]): [M] lower bounds (A); the band
            [lower, upper] holds the free chains at the placed geometry —
            neither drifting away nor penetrating the receptor wall.

    Returns:
        torch.Tensor: the denoised coordinates of x in inference stage
            [..., N_sample, N_atom, 3]
    """
    N_atom = input_feature_dict["atom_to_token_idx"].size(-1)
    batch_shape = s_inputs.shape[:-2]
    device = s_inputs.device
    dtype = s_inputs.dtype
    tfg_cfg = parse_tfg_config(guidance_configs)
    if tfg_cfg.enable:
        logger.info("Guidance is enabled.")
        # fp32: TFG math (projections, linalg) requires it; see TFGEngine.step.
        tfg = TFGEngine(tfg_cfg, device=device, dtype=torch.float32)

    _anchor_state = None
    if (
        pin_mask is not None
        and init_coords is not None
        and anchor_index is not None
        and anchor_upper is not None
    ):
        # Precomputed hard-anchor projector: damped Jacobi projection of the
        # anchor distance pairs back inside their band [lower, upper], moving
        # FREE atoms only (pinned atoms are constants). A direct minimum-norm
        # linalg solve goes singular here when many pairs share free atoms
        # (measured: coordinates blown up by hundreds of A); the damped
        # per-pair form is unconditionally stable and converges in a few tens
        # of sweeps on step-sized violations. Without the lower bound the
        # projection drives the peptide INTO the receptor wall (measured 0.25
        # A clashes); the band holds the placed geometry from both sides.
        _pin_col = pin_mask.to(device=device, dtype=torch.float32)
        _free_col = 1.0 - _pin_col
        _a_idx = anchor_index.to(device=device, dtype=torch.long)
        _a_up = anchor_upper.to(device=device, dtype=torch.float32)
        _a_lo = (
            anchor_lower.to(device=device, dtype=torch.float32)
            if anchor_lower is not None
            else torch.zeros_like(_a_up)
        )
        _active = (_free_col[_a_idx[0]] + _free_col[_a_idx[1]]) > 0
        _a_idx = _a_idx[:, _active]
        _a_up = _a_up[_active]
        _a_lo = _a_lo[_active]
        _wi = _free_col[_a_idx[0]]
        _wj = _free_col[_a_idx[1]]

        def _anchor_project(x_l: torch.Tensor, _iters: int = 30) -> None:
            if _a_idx.numel() == 0:
                return
            shape = x_l.shape
            xs = x_l.reshape(-1, shape[-2], 3).float()
            for si in range(xs.shape[0]):
                x = xs[si]
                for _ in range(_iters):
                    a = x[_a_idx[0]]
                    b = x[_a_idx[1]]
                    d = (a - b).norm(dim=-1)
                    # signed violation: positive = too far (shrink), negative
                    # = too close (expand)
                    viol = torch.where(d > _a_up, d - _a_up, (d - _a_lo).clamp(max=0))
                    if float(viol.abs().max()) < 1e-3:
                        break
                    u = (a - b) / d.clamp(min=1e-8).unsqueeze(-1)
                    corr = torch.zeros_like(x)
                    # Bound each sweep's displacement: a linearized projection
                    # is only valid for step-sized violations; degenerate
                    # upstream geometry would otherwise teleport atoms (the
                    # TFG linalg solver had the same failure mode before its
                    # clamp — measured a Cys SG pushed 32 A). Convergence
                    # still takes a few extra sweeps.
                    half = 0.5 * viol.clamp(-4.0, 4.0)
                    corr.index_add_(0, _a_idx[0], (-half).unsqueeze(-1) * u * _wi.unsqueeze(-1))
                    corr.index_add_(0, _a_idx[1], (+half).unsqueeze(-1) * u * _wj.unsqueeze(-1))
                    # Per-ATOM cap: many anchor pairs share one atom (the
                    # pocket pairs all touch the same SG/CB on the anchored
                    # residues); index_add_ accumulates their corrections
                    # without bound and one sweep can fling the atom tens of
                    # A (measured: Cys1 CA-CB 17-32 A). Cap the per-atom step
                    # to the sweep scale. (A/B switch for the confidence
                    # non-regression study; default on.)
                    corr_norm = corr.norm(dim=-1, keepdim=True)
                    corr = corr * (corr_norm.clamp(max=0.5) / corr_norm.clamp(min=1e-8))
                    x += corr
                xs[si] = x
            x_l.copy_(xs.to(dtype).reshape(shape))

        _anchor_state = {"project": _anchor_project}
        logger.info(
            f"Hard anchor projection enabled: {int(_a_idx.shape[1])} band pairs."
        )

    _chiral_state = None
    if chiral_quads is not None and chiral_sign is not None:
        # Backbone chirality guard: the denoiser can locally flip a residue's
        # stereochemistry while folding a free chain (measured: +2.54 CA volume
        # on an L-designed peptide, product-gate rejected). The CA chirality is
        # exactly the side of the N-CA-C plane the CB sits on, so reflecting CB
        # across that plane restores the designed sign with ZERO backbone
        # perturbation. Runs after the pin + anchor projections each step.
        _q = chiral_quads.to(device=device, dtype=torch.long)  # [4, M]
        _s = chiral_sign.to(device=device, dtype=torch.float32)  # [M]

        def _chirality_project(x_l: torch.Tensor) -> int:
            shape = x_l.shape
            xs = x_l.reshape(-1, shape[-2], 3).float()
            flipped = 0
            for si in range(xs.shape[0]):
                x = xs[si]
                n = x[_q[0]] - x[_q[1]]          # N - CA
                c = x[_q[2]] - x[_q[1]]          # C - CA
                normal = torch.cross(n, c, dim=-1)
                norm = normal.norm(dim=-1, keepdim=True).clamp(min=1e-8)
                n_hat = normal / norm
                cb = x[_q[3]] - x[_q[1]]         # CB - CA
                signed = (cb * n_hat).sum(dim=-1)  # signed distance along normal
                cb_len = cb.norm(dim=-1)
                # Guard both defect classes: stereochemistry flips AND
                # stretched CA-CB bonds (2.6-3.2 A with the sign intact).
                # Any flagged residue gets CB rebuilt at the standard 1.53 A
                # along a direction that keeps/repairs the designed sign.
                bad = ((torch.sign(signed) != torch.sign(_s)) & (signed.abs() > 1e-6)) | \
                      (cb_len > 1.8) | (cb_len < 1.2)
                if bool(bad.any()):
                    flipped += int(bad.sum().item())
                    # REBUILD rather than reflect: a plain reflection keeps
                    # whatever deformed CA-CB distance the earlier
                    # projections left behind. Rebuilding at the standard
                    # 1.53 A along the sign-corrected direction restores the
                    # designed sign and a valid bond in one shot.
                    idx_bad = _q[3][bad]
                    direction = cb[bad] - 2.0 * (cb[bad] * n_hat[bad]).sum(
                        dim=-1, keepdim=True) * n_hat[bad] * \
                        (torch.sign(signed[bad]) != torch.sign(_s[bad])).float().unsqueeze(-1)
                    direction = direction / direction.norm(
                        dim=-1, keepdim=True).clamp(min=1e-8)
                    x[idx_bad] = x[_q[1]][bad] + 1.53 * direction
                xs[si] = x
            x_l.copy_(xs.to(dtype).reshape(shape))
            return flipped

        _chiral_state = {"project": _chirality_project}
        logger.info(
            f"Backbone chirality guard enabled: {int(_q.shape[1])} residues."
        )

    def _chunk_sample_diffusion(chunk_n_sample, inplace_safe, chunk_offset=0):
        # init noise
        # [..., N_sample, N_atom, 3]
        # init_coords may be [N_atom, 3] (broadcast to every sample) or
        # [n_sample, N_atom, 3] (per-sample starts, e.g. a docking placement
        # ensemble): under chunked sampling each chunk takes its own slice.
        # The pinned geometry is identical across samples, so all
        # pin/centering/chirality references use the first sample's rows.
        init_base = None
        chunk_init = None
        if init_coords is not None:
            init_base = (init_coords[0] if init_coords.dim() == 3
                         else init_coords).to(device=device, dtype=dtype)
            if (init_coords.dim() == 3
                    and chunk_offset + chunk_n_sample <= init_coords.shape[0]):
                chunk_init = init_coords[
                    chunk_offset:chunk_offset + chunk_n_sample
                ].to(device=device, dtype=dtype)
        if init_coords is not None:
            if chunk_init is not None and chunk_init.shape[0] == chunk_n_sample:
                base = chunk_init.reshape(
                    (1,) * len(batch_shape) + (chunk_n_sample, N_atom, 3)
                )
            else:
                base = init_base.view(
                    (1,) * (len(batch_shape) + 1) + (N_atom, 3)
                )
            noise_full = noise_schedule[0] * torch.randn(
                size=(*batch_shape, chunk_n_sample, N_atom, 3), device=device, dtype=dtype
            )
            if init_mask is not None:
                keep = init_mask.to(device=device, dtype=dtype).view(
                    (1,) * (len(batch_shape) + 1) + (N_atom, 1)
                )
            else:
                keep = 1.0
            x_l = keep * (base + init_noise_scale * noise_full) + (1.0 - keep) * noise_full
        else:
            x_l = noise_schedule[0] * torch.randn(
                size=(*batch_shape, chunk_n_sample, N_atom, 3), device=device, dtype=dtype
            )  # NOTE: set seed in distributed training

        _nan_dbg = bool(os.environ.get("PROTENIX_TFG_DEBUG_NAN", ""))
        for step_i, (c_tau_last, c_tau) in enumerate(
            zip(noise_schedule[:-1], noise_schedule[1:])
        ):
            if _nan_dbg and not bool(torch.isfinite(x_l).all()):
                logger.warning(
                    f"SAMPLER-NAN entry step={step_i} nonfinite="
                    f"{int((~torch.isfinite(x_l)).sum().item())}"
                )
            if pin_mask is not None and init_coords is not None:
                # Inpainting: the pinned atoms define the frame. The stock
                # random SE(3) augmentation would kick the free part in a
                # random orientation every step while the pin snaps the
                # receptor back — the free chains drift away and lose
                # chirality (measured: 16 A displacement on 3LNJ). Recenter
                # on the PINNED centroid only: the receptor stays at its
                # absolute position (the pin below becomes a no-op) and the
                # free part keeps its relative geometry. Recentering on the
                # whole-complex centroid instead would translate the peptide
                # by (pinned_center - complex_center) every step (measured
                # ~9 A on 3LNJ) — the pin discards that shift for the
                # receptor but leaves it on the free chains.
                _pin_step = pin_mask.to(device=device, dtype=dtype).view(1, N_atom, 1)
                _base_step = init_base.view(1, N_atom, 3)
                _ref_center = (
                    (_base_step * _pin_step).sum(dim=1, keepdim=True)
                    / _pin_step.sum().clamp(min=1.0)
                )
                _cur_center = (
                    (x_l * _pin_step).sum(dim=-2, keepdim=True)
                    / _pin_step.sum().clamp(min=1.0)
                )
                x_l = x_l - _cur_center + _ref_center
                x_l = x_l.to(dtype)
            else:
                # [..., N_sample, N_atom, 3]
                x_l = (
                    centre_random_augmentation(x_input_coords=x_l, N_sample=1)
                    .squeeze(dim=-3)
                    .to(dtype)
                )

            # Denoise with a predictor-corrector sampler
            # 1. Add noise to move x_{c_tau_last} to x_{t_hat}
            gamma = float(gamma0) if c_tau > gamma_min else 0
            t_hat = c_tau_last * (gamma + 1)

            delta_noise_level = torch.sqrt(t_hat**2 - c_tau_last**2)
            x_noisy = x_l + noise_scale_lambda * delta_noise_level * torch.randn(
                size=x_l.shape, device=device, dtype=dtype
            )

            # 2. Denoise from x_{t_hat} to x_{c_tau}
            # Euler step only
            t_hat = (
                t_hat.reshape((1,) * (len(batch_shape) + 1))
                .expand(*batch_shape, chunk_n_sample)
                .to(dtype)
            )

            if tfg_cfg.enable:
                x_l = tfg.step(
                    denoise_net,
                    x=x_noisy,
                    t_hat=t_hat,
                    input_feature_dict=input_feature_dict,
                    s_inputs=s_inputs,
                    s_trunk=s_trunk,
                    z_trunk=z_trunk,
                    pair_z=pair_z,
                    p_lm=p_lm,
                    c_l=c_l,
                    chunk_size=attn_chunk_size,
                    inplace_safe=inplace_safe,
                    enable_efficient_fusion=enable_efficient_fusion,
                    c_tau=c_tau,
                    step_i=step_i,
                    num_diffusion_steps=len(noise_schedule) - 1,
                    step_scale_eta=step_scale_eta,
                )
            else:
                x_denoised = denoise_net(
                    x_noisy=x_noisy,
                    t_hat_noise_level=t_hat,
                    input_feature_dict=input_feature_dict,
                    s_inputs=s_inputs,
                    s_trunk=s_trunk,
                    z_trunk=z_trunk,
                    pair_z=pair_z,
                    p_lm=p_lm,
                    c_l=c_l,
                    chunk_size=attn_chunk_size,
                    inplace_safe=inplace_safe,
                    enable_efficient_fusion=enable_efficient_fusion,
                )

                delta = (x_noisy - x_denoised) / t_hat[
                    ..., None, None
                ]  # Line 9 of AF3 uses 'x_l_hat' instead, which we believe  is a typo.
                dt = c_tau - t_hat
                x_l = x_noisy + step_scale_eta * dt[..., None, None] * delta

            # True inpainting: clamp the pinned atoms to the input coordinates
            # after every step (the step's recentering moved them; restore the
            # absolute frame before the next iteration).
            if pin_mask is not None and init_coords is not None:
                _pin = pin_mask.to(device=device, dtype=dtype).view(
                    (1,) * (len(batch_shape) + 1) + (N_atom, 1)
                )
                _base = init_base.view(
                    (1,) * (len(batch_shape) + 1) + (N_atom, 3)
                )
                x_l = x_l * (1.0 - _pin) + _base * _pin
                # Hard geometric anchor on the free part: the denoiser's x0
                # prior pulls an unanchored peptide to its own pocket guess
                # (measured 8 A off a crystal pose) and the TFG projection
                # only fires on the x0 prediction, not on x_t. Project the
                # anchor pairs back inside their bounds on x_t itself, with
                # the pinned atoms held as constants (minimum-norm solution
                # over the free columns only). Step-sized displacements keep
                # the linearization exact.
                if _anchor_state is not None:
                    _anchor_state["project"](x_l)
                if _chiral_state is not None:
                    _chiral_state["project"](x_l)
                if _nan_dbg and not bool(torch.isfinite(x_l).all()):
                    logger.warning(
                        f"SAMPLER-NAN post-pin step={step_i} nonfinite="
                        f"{int((~torch.isfinite(x_l)).sum().item())}"
                    )

        if pin_mask is not None and init_coords is not None:
            _pin = pin_mask.to(device=device, dtype=dtype).view(
                (1,) * (len(batch_shape) + 1) + (N_atom, 1)
            )
            _base = init_base.view(
                (1,) * (len(batch_shape) + 1) + (N_atom, 3)
            )
            x_l = x_l * (1.0 - _pin) + _base * _pin
            if _anchor_state is not None:
                _anchor_state["project"](x_l)
            if _chiral_state is not None:
                _chiral_state["project"](x_l)

        return x_l

    if diffusion_chunk_size is None:
        x_l = _chunk_sample_diffusion(N_sample, inplace_safe=inplace_safe)
    else:
        x_l = []
        no_chunks = N_sample // diffusion_chunk_size + (
            N_sample % diffusion_chunk_size != 0
        )
        for i in range(no_chunks):
            chunk_n_sample = (
                diffusion_chunk_size
                if i < no_chunks - 1
                else N_sample - i * diffusion_chunk_size
            )
            chunk_x_l = _chunk_sample_diffusion(
                chunk_n_sample, inplace_safe=inplace_safe,
                chunk_offset=i * diffusion_chunk_size,
            )
            x_l.append(chunk_x_l)
        x_l = torch.cat(x_l, -3)  # [..., N_sample, N_atom, 3]
    return x_l


def sample_diffusion_training(
    noise_sampler: TrainingNoiseSampler,
    denoise_net: Callable,
    label_dict: dict[str, Any],
    input_feature_dict: dict[str, Any],
    s_inputs: torch.Tensor,
    s_trunk: torch.Tensor,
    z_trunk: torch.Tensor,
    pair_z: torch.Tensor,
    p_lm: torch.Tensor,
    c_l: torch.Tensor,
    N_sample: int = 1,
    diffusion_chunk_size: Optional[int] = None,
    use_conditioning: bool = True,
    enable_efficient_fusion: bool = False,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Implements diffusion training as described in AF3 Appendix at page 23.
    It performances denoising steps from time 0 to time T.
    The time steps (=noise levels) are given by noise_schedule.

    Args:
        noise_sampler (TrainingNoiseSampler): sampler for training noise-level.
        denoise_net (Callable): the network that performs the denoising step.
        label_dict (dict[str, Any]) : a dictionary containing the followings.
            "coordinate": the ground-truth coordinates
                [..., N_atom, 3]
            "coordinate_mask": whether true coordinates exist.
                [..., N_atom]
        input_feature_dict (dict[str, Any]): input meta feature dict
        s_inputs (torch.Tensor): single embedding from InputFeatureEmbedder
            [..., N_tokens, c_s_inputs]
        s_trunk (torch.Tensor): single feature embedding from PairFormer (Alg17)
            [..., N_tokens, c_s]
        z_trunk (torch.Tensor): pair feature embedding from PairFormer (Alg17)
            [..., N_tokens, N_tokens, c_z]
        pair_z (torch.Tensor): pair feature embedding from InputFeatureEmbedder
            [..., N_tokens, N_tokens, c_z_inputs]
        p_lm (torch.Tensor): MSA embedding
            [..., N_tokens, c_p_lm]
        c_l (torch.Tensor): ligand embedding
            [..., N_tokens, c_c_l]
        N_sample (int): number of training samples
        diffusion_chunk_size (Optional[int]): Chunk size for diffusion operation. Defaults to None.
        use_conditioning (bool): Whether to use conditioning. Defaults to True.
        enable_efficient_fusion (bool): Whether to enable efficient fusion. Defaults to False.

    Returns:
        tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
            x_gt_augment: the augmented ground-truth coordinates [..., N_sample, N_atom, 3]
            x_denoised: the denoised coordinates [..., N_sample, N_atom, 3]
            sigma: the sampled noise-level [..., N_sample]
    """
    batch_size_shape = label_dict["coordinate"].shape[:-2]
    device = label_dict["coordinate"].device
    dtype = label_dict["coordinate"].dtype
    # Areate N_sample versions of the input structure by randomly rotating and translating
    x_gt_augment = centre_random_augmentation(
        x_input_coords=label_dict["coordinate"],
        N_sample=N_sample,
        mask=label_dict["coordinate_mask"],
    ).to(
        dtype
    )  # [..., N_sample, N_atom, 3]

    # Add independent noise to each structure
    # sigma: independent noise-level [..., N_sample]
    sigma = noise_sampler(size=(*batch_size_shape, N_sample), device=device).to(dtype)
    # noise: [..., N_sample, N_atom, 3]
    noise = torch.randn_like(x_gt_augment, dtype=dtype) * sigma[..., None, None]

    # Get denoising outputs [..., N_sample, N_atom, 3]
    if diffusion_chunk_size is None:
        x_denoised = denoise_net(
            x_noisy=x_gt_augment + noise,
            t_hat_noise_level=sigma,
            input_feature_dict=input_feature_dict,
            s_inputs=s_inputs,
            s_trunk=s_trunk,
            z_trunk=z_trunk,
            pair_z=pair_z,
            p_lm=p_lm,
            c_l=c_l,
            use_conditioning=use_conditioning,
            enable_efficient_fusion=enable_efficient_fusion,
        )
    else:
        x_denoised = []
        no_chunks = N_sample // diffusion_chunk_size + (
            N_sample % diffusion_chunk_size != 0
        )
        for i in range(no_chunks):
            x_noisy_i = (x_gt_augment + noise)[
                ..., i * diffusion_chunk_size : (i + 1) * diffusion_chunk_size, :, :
            ]
            t_hat_noise_level_i = sigma[
                ..., i * diffusion_chunk_size : (i + 1) * diffusion_chunk_size
            ]
            x_denoised_i = denoise_net(
                x_noisy=x_noisy_i,
                t_hat_noise_level=t_hat_noise_level_i,
                input_feature_dict=input_feature_dict,
                s_inputs=s_inputs,
                s_trunk=s_trunk,
                z_trunk=z_trunk,
                pair_z=pair_z,
                p_lm=p_lm,
                c_l=c_l,
                use_conditioning=use_conditioning,
                enable_efficient_fusion=enable_efficient_fusion,
            )
            x_denoised.append(x_denoised_i)
        x_denoised = torch.cat(x_denoised, dim=-3)

    return x_gt_augment, x_denoised, sigma
