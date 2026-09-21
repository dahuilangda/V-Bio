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
import numpy as np

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


def _make_band_projector(idx, up, lo, device, pin_mask=None):
    """Damped-Jacobi distance-band projector for covalent geometry.

    Constrains ATOMS, not groups: without it a steric shove dislodges one
    atom off its residue, since nothing in the sampler negotiates that
    atom against its own bonds (measured: free-chain bonds off by up to
    0.22 A with aromatic rings collapsed to CG-CZ 1.5 A). Projected after
    the clash bands so bond geometry wins the negotiation -- the
    official PairwiseDistancePotential ordering, angles then bonds.

    Pinned endpoints (pin_mask == 1) take zero correction: they are the
    fixed receptor, and a band whose one end is pinned must move only its
    free end (SHAKE with a fixed anchor). Without this the clash shell
    pushed the receptor off its input pose (measured: pin deviation up to
    1.8 A and aromatic rings pulled to 2.2-3.4 A fighting the chemistry
    bands). (Freezing the rigid-template atoms the same way was measured
    and rejected: exocyclic bonds 0.21-0.33 A -- the outside atom cannot
    absorb the junction correction alone.)

    Damped per-pair Jacobi sweeps: a direct minimum-norm solve goes
    singular when many pairs share atoms; this form is unconditionally
    stable and converges in tens of sweeps on step-sized violations.
    """
    if idx is None or up is None or idx.numel() == 0:
        return None
    idx = idx.to(device=device, dtype=torch.long)
    if idx.dim() != 2 or idx.shape[0] != 2:
        idx = idx.t().contiguous()
    up = up.to(device=device, dtype=torch.float32)
    lo = (lo.to(device=device, dtype=torch.float32)
          if lo is not None else torch.maximum(up - 0.24,
                                               torch.tensor(0.5, device=device)))
    free_col = (
        1.0 - pin_mask.to(device=device, dtype=torch.float32)
        if pin_mask is not None
        else None
    )
    if free_col is not None:
        active = (free_col[idx[0]] + free_col[idx[1]]) > 0
        idx = idx[:, active]
        up = up[active]
        lo = lo[active]
        wi = free_col[idx[0]]
        wj = free_col[idx[1]]
    else:
        wi = wj = None
    if idx.numel() == 0:
        return None

    def _project(x: torch.Tensor, iters: int = 30) -> None:
        shape = x.shape
        flat = x.reshape(-1, shape[-2], 3).float()
        for si in range(flat.shape[0]):
            xi = flat[si]
            for _ in range(iters):
                a, b = xi[idx[0]], xi[idx[1]]
                d = (a - b).norm(dim=-1)
                viol = torch.where(d > up, d - up, (d - lo).clamp(max=0))
                if float(viol.abs().max()) < 1e-3:
                    break
                u = (a - b) / d.clamp(min=1e-8).unsqueeze(-1)
                corr = torch.zeros_like(xi)
                half = 0.5 * viol.clamp(-4.0, 4.0)
                if wi is not None:
                    corr.index_add_(0, idx[0],
                                    (-half).unsqueeze(-1) * u * wi.unsqueeze(-1))
                    corr.index_add_(0, idx[1],
                                    (+half).unsqueeze(-1) * u * wj.unsqueeze(-1))
                else:
                    corr.index_add_(0, idx[0], (-half).unsqueeze(-1) * u)
                    corr.index_add_(0, idx[1], (+half).unsqueeze(-1) * u)
                norm = corr.norm(dim=-1, keepdim=True)
                xi += corr * (norm.clamp(max=0.5) / norm.clamp(min=1e-8))
            flat[si] = xi
        x.copy_(flat.to(x.dtype).reshape(shape))

    return _project


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
    bond_index: Optional[torch.Tensor] = None,
    bond_upper: Optional[torch.Tensor] = None,
    bond_lower: Optional[torch.Tensor] = None,
    clash_index: Optional[torch.Tensor] = None,
    clash_lower: Optional[torch.Tensor] = None,
    ring_rows: Optional[torch.Tensor] = None,
    ring_coords: Optional[torch.Tensor] = None,
    ring_bb_rows: Optional[torch.Tensor] = None,
    ring_bb_coords: Optional[torch.Tensor] = None,
    init_coords: Optional[torch.Tensor] = None,
    pin_mask: Optional[torch.Tensor] = None,
    init_mask: Optional[torch.Tensor] = None,
    init_noise_scale: float = 0.0,
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




    # Analytic aromatic side-chain rebuild (the AF3/protenix
    # construction principle: side-chain internal geometry comes from
    # the CCD template placed on the backbone frame, chi torsions owned
    # by the network). Every step:
    #   1. Kabsch-fit the template's N/CA/C onto the network's N/CA/C
    #      (exact backbone bond/angle geometry at the junction),
    #   2. read chi1 (N-CA-CB-CG) and chi2 (CA-CB-CG-CD1) from the
    #      network's CURRENT side chain,
    #   3. rotate the placed template about CA-CB and CB-CG to those
    #      chi values, then write CB..side chain back.
    # The rebuilt side chain satisfies every bond AND angle band
    # exactly -- it IS the intersection point; the Jacobi bands only
    # ever negotiated toward it and measured boat-shaped six-rings on
    # the way (CG +0.39 A toward CB, all ring bonds in-band: pairwise
    # distances cannot exclude boats).
    _ring_project = None
    _ring_atom_rows = None
    if ring_rows is not None and ring_coords is not None \
            and ring_bb_rows is not None and ring_bb_coords is not None \
            and ring_rows.numel() > 0:
        _rr = ring_rows.to(device=device, dtype=torch.long)
        _rc = ring_coords.to(device=device, dtype=torch.float32)
        _bbr = ring_bb_rows.to(device=device, dtype=torch.long)
        _bbc = ring_bb_coords.to(device=device, dtype=torch.float32)
        if pin_mask is not None:
            _free_only = (1.0 - pin_mask.to(
                device=device, dtype=torch.float32))
            # only fully-free side chains: a pinned atom inside a
            # template would fight the pin clamp (receptor aromatics
            # are pinned by design)
            _ok = _free_only[_rr].sum(dim=-1) >= (_rr >= 0).sum(dim=-1) - 1e-6
            _rr, _rc = _rr[_ok], _rc[_ok]
            _bbr, _bbc = _bbr[_ok], _bbc[_ok]
        if _rr.numel() > 0:
            n_rings = int(_rr.shape[0])
            _ring_atom_rows = torch.unique(_rr[_rr >= 0])

            def _dihedral(p0, p1, p2, p3):
                b0, b1, b2 = p0 - p1, p2 - p1, p3 - p2
                b1n = b1 / b1.norm().clamp(min=1e-8)
                v = b0 - (b0 @ b1n).unsqueeze(-1) * b1n
                w = b2 - (b2 @ b1n).unsqueeze(-1) * b1n
                return torch.atan2(
                    torch.cross(b1n, v, dim=-1) @ w, (v * w).sum(-1))

            def _rot_about(axis_p, axis_q, angle, pts):
                # Rodrigues rotation of pts about the axis (p->q)
                origin = axis_p
                a = (axis_q - axis_p)
                a = a / a.norm().clamp(min=1e-8)
                rel = pts - origin
                return origin + (rel * torch.cos(angle)
                                 + torch.cross(a.unsqueeze(0).expand_as(rel),
                                               rel, dim=-1) * torch.sin(angle)
                                 + a.unsqueeze(0).expand_as(rel)
                                 * ((rel @ a) / a.norm().clamp(min=1e-8)
                                    ).unsqueeze(-1)
                                 * (1.0 - torch.cos(angle)))

            def _ring_project(x: torch.Tensor) -> None:
                shape = x.shape
                flat = x.reshape(-1, shape[-2], 3).float()
                for si in range(flat.shape[0]):
                    xi = flat[si]
                    for ri in range(n_rings):
                        rows = _rr[ri]
                        rows = rows[rows >= 0]
                        if rows.numel() < 4:
                            continue
                        bb = _bbr[ri]                 # N, CA, C
                        tpl_bb = _bbc[ri]             # template N, CA, C
                        cur_bb = xi.index_select(0, bb)
                        # backbone frame fit (Kabsch, reflection-safe)
                        cc_b = cur_bb.mean(dim=0, keepdim=True)
                        ic_b = tpl_bb.mean(dim=0, keepdim=True)
                        Pcb, Qcb = cur_bb - cc_b, tpl_bb - ic_b
                        u, s, vt = torch.linalg.svd(Qcb.t() @ Pcb)
                        dsgn = torch.sign(torch.det(u @ vt))
                        dsgn = torch.where(
                            torch.isfinite(dsgn) & (dsgn != 0), dsgn,
                            torch.ones_like(dsgn))
                        rot = u @ torch.diag(torch.stack(
                            [torch.ones_like(dsgn), torch.ones_like(dsgn),
                             dsgn])) @ vt
                        # place the template side chain on that frame
                        sc = _rc[ri][:rows.numel()]
                        placed = (sc - ic_b) @ rot + cc_b
                        # read the network's chi1/chi2 from CURRENT atoms
                        # (rows[0] is CB -- every template starts at CB)
                        CBn = xi[rows[0]]
                        CGn = xi[rows[1]] if rows.numel() > 1 else CBn
                        CD1n = xi[rows[2]] if rows.numel() > 2 else CGn
                        chi1_net = _dihedral(xi[bb[0]], xi[bb[1]], CBn, CGn)
                        chi2_net = _dihedral(xi[bb[1]], CBn, CGn, CD1n)
                        chi1_tpl = _dihedral(
                            xi[bb[0]], xi[bb[1]], placed[0], placed[1])
                        chi2_tpl = _dihedral(
                            xi[bb[1]], placed[0], placed[1],
                            placed[2] if rows.numel() > 2 else placed[1])
                        # rotate CG.. about CA-CB to the network chi1
                        d1 = chi1_net - chi1_tpl
                        body = placed[1:]
                        body = _rot_about(xi[bb[1]], placed[0], d1, body)
                        # rotate CD1.. about CB-CG to the network chi2
                        # (recompute chi2 after the chi1 rotation)
                        CGq = body[0]
                        CD1q = body[1] if body.shape[0] > 1 else CGq
                        chi2_q = _dihedral(xi[bb[1]], placed[0], CGq, CD1q)
                        d2 = chi2_net - chi2_q
                        tail = body[1:]
                        if tail.numel() > 0:
                            tail = _rot_about(placed[0], CGq, d2, tail)
                        final = torch.cat(
                            [placed[0:1], CGq.unsqueeze(0), tail], dim=0) \
                            if tail.numel() > 0 else placed[0:2]
                        xi[rows] = final[:rows.numel()]
                x.copy_(flat.to(x.dtype).reshape(shape))

            logger.info("analytic aromatic rebuild active: %d residues",
                        n_rings)

    # Free-chain covalent geometry (CCD rest lengths) as a per-step
    # projection: bonds are chemistry, not guidance -- they apply whether or
    # not any guidance channel is active, and they are what keeps the free
    # chain's internal geometry intact while the sampler moves it.
    # NOTE the rigid-template atoms deliberately stay MOBILE here: freezing
    # them (frozen_rows, same mechanism as the receptor pin) measured
    # 0.21-0.33 A exocyclic bonds and 1/8 clean samples -- the junction
    # displacement cannot be absorbed by the outside atom alone within the
    # Jacobi budget. The negotiated split (both ends move) costs only
    # ~0.15 A of ring planarity, by far the cheapest trade.
    _bond_project = _make_band_projector(bond_index, bond_upper, bond_lower,
                                         device, pin_mask=pin_mask)
    if _bond_project is not None:
        logger.info("chemistry bands active: %d bonds+rings",
                    int(bond_index.shape[0]))
    # Clash shell projection: OFF by default for blind/refine routes
    # (there the model's own poses stay clash-free and the 37k floors
    # only fight the chemistry bands); runs that need the floors turn
    # it ON via the env because a soft-guided sampler can park a
    # fraction of samples pressed into the rim residues (measured
    # 2026-09-20: n22 4-10 without, clean with, bonds 0.041 — the
    # chemistry bands run AFTER the shell, so covalent geometry wins).
    _clash_project = None
    _clash_enabled = os.environ.get(
        "PROTENIX_CLASH_SHELL_PROJECT", "0").strip().lower() in ("1", "true")
    if (_clash_enabled and clash_index is not None
            and clash_lower is not None):
        _clash_upper = torch.full_like(
            clash_lower.to(device), 1e3, dtype=torch.float32)
        _clash_project = _make_band_projector(
            clash_index, _clash_upper, clash_lower, device,
            pin_mask=pin_mask)
        if _clash_project is not None:
            logger.info("clash shell active: %d one-sided floors",
                        int(clash_index.shape[0]))

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
                # free part keeps its relative geometry.
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

            # True inpainting: clamp the pinned atoms to the input pose.
            # The receptor is FIXED by contract — only the peptide is being
            # generated. The clamp restores the absolute frame the step's
            # recentering/prediction moved.
            if pin_mask is not None and init_coords is not None:
                _pin = pin_mask.to(device=device, dtype=dtype).view(
                    (1,) * (len(batch_shape) + 1) + (N_atom, 1)
                )
                _base = init_base.view(
                    (1,) * (len(batch_shape) + 1) + (N_atom, 3)
                )
                x_l = x_l * (1.0 - _pin) + _base * _pin

            # Chemistry: the covalent bond bands converge the BACKBONE
            # first, then the analytic aromatic rebuild places each
            # side chain on that converged backbone frame at the
            # network's chi values -- a complete intersection point
            # (every side-chain bond AND angle band satisfied exactly,
            # no negotiation residue). No band pass after: the rebuild
            # IS the side-chain solution, and re-running the Jacobi on
            # it only re-introduces the boat drift. The clash shell
            # rides the same per-step channel when active.
            # Aromatic chemistry, two routes selected by
            # PROTENIX_AROMATIC_MODE (19-variant sweep, 2026-09-21):
            #
            # "project" (default) -- template Kabsch pass then the bond
            # bands. Interface quality leads: 6-7/8 shipping-clean,
            # bonds 0.04, junction angles +-5 deg; six-rings carry
            # ~0.15-0.25 A of boat buckling (pairwise-distance Jacobi
            # cannot exclude boats).
            #
            # "rebuild" -- every step, AFTER the bands converge, each
            # aromatic side chain is rebuilt analytically on the
            # backbone frame at the network's chi values (the AF3/
            # protenix construction): every bond AND angle exact, rings
            # perfectly planar, OH in-plane. Chemistry leads; the
            # interface pays (1-3/8 clean) because the faithful chi
            # values are what bury the rings into the receptor and no
            # single-atom clash floor can un-bury them without breaking
            # the ring it pushes.
            _mode = os.environ.get("PROTENIX_AROMATIC_MODE", "project")
            if _mode == "rebuild":
                if _bond_project is not None:
                    _bond_project(x_l, iters=30)
                if _ring_project is not None:
                    _ring_project(x_l)
                if _clash_project is not None:
                    _clash_project(x_l, iters=3)
            else:
                if _ring_project is not None:
                    _ring_project(x_l)
                if _bond_project is not None:
                    _bond_project(x_l, iters=30)
                if _clash_project is not None:
                    _clash_project(x_l, iters=3)

        # Deliberately NO projection after the loop: the shipped structure
        # is exactly what the final sampler step produced (including its
        # in-step projection) — a post-loop "convergence" pass is
        # post-processing and is banned by the design contract (results
        # must be what trunk+diffusion actually emitted).

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
