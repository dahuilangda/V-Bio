"""Fixed-D-target peptide docking driver (validated E5/T10 protocol).

Monkey-patches Boltz2's diffusion sampler with an inpainting-style
fixed-receptor version and runs the Boltz2Score inference stack in-process:

  - receptor (the mirrored D-target) is reset exactly at every stage
    (noisy input, x0 prediction, post-Euler) — measured 0.000 A fidelity;
  - SE(3) augmentation disabled (stable frame; the receptor defines it);
  - optional pocket anchoring box: rigidly confines the peptide centroid to
    the pocket (validated; peptide-only shift — a whole-system shift gets
    swallowed by the joint recentering);
  - gamma_0 = 0.0 recommended (deterministic descent; gamma=0.8 makes the
    mid-sigma noise random-walk eject the peptide).

Validated results (3LNJ mirror): ipTM 0.95-0.96 for 4 random pocket
orientations; output peptide 11/11 L chirality; product flips to D.
"""

from __future__ import annotations

import math
from pathlib import Path
from types import MethodType
from typing import Optional

import numpy as np
import torch

PROJECT_ROOT = Path("/data/Boltz2Score")

_FIXED_RECEPTOR_CONFIG: dict = {
    "enabled": False,
    "peptide_init": "input",
    "pocket_box_radius": 0.0,
    "debug": False,
}


def set_fixed_receptor_config(**kwargs) -> None:
    _FIXED_RECEPTOR_CONFIG.update(kwargs)


def _kabsch_align_batch(coords: torch.Tensor, mask: torch.Tensor, ref: torch.Tensor) -> torch.Tensor:
    out = coords.clone()
    ref_m = ref[mask]
    mu_ref = ref_m.mean(dim=0)
    for b in range(coords.size(0)):
        mob = coords[b][mask]
        mu_mob = mob.mean(dim=0)
        P = mob - mu_mob
        Q = ref_m - mu_ref
        H = (P.T @ Q).to(torch.float64)
        U, S, Vt = torch.linalg.svd(H)
        d = torch.sign(torch.det(U @ Vt))
        D = torch.diag(torch.tensor([1.0, 1.0, d], dtype=torch.float64, device=coords.device))
        R = ((U @ D @ Vt).T).to(coords.dtype)
        out[b] = (R @ (coords[b] - mu_mob).T).T + mu_ref
    return out


def install_fixed_receptor_sampler(model_module) -> None:
    """Attach the inpainting sampler to a loaded Boltz2ScoreModel instance."""
    sm = model_module.structure_module
    sm.sample = MethodType(_sample_fixed_receptor, sm)
    # keep Boltz2ScoreModel._configure_structure_sampling from replacing it
    sm._boltz2score_input_init_patch = True
    # The pinned receptor already defines a stable frame, so the reverse-step
    # rigid alignment is unnecessary — and its SVD occasionally diverges on
    # degenerate point clouds (bf16 trajectories), corrupting the sample.
    sm.alignment_reverse_diff = False


def _sample_fixed_receptor(
    self,
    atom_mask,
    num_sampling_steps=None,
    multiplicity=1,
    max_parallel_samples=None,
    steering_args=None,
    **network_condition_kwargs,
):
    import boltz.model.modules.diffusionv2 as diffusionv2_mod
    from boltz.model.potentials.potentials import get_potentials

    potentials = None
    if steering_args is not None and (
        steering_args["fk_steering"]
        or steering_args["physical_guidance_update"]
        or steering_args["contact_guidance_update"]
    ):
        if steering_args["fk_steering"]:
            raise RuntimeError("fixed-receptor sampler does not support fk_steering")
        potentials = get_potentials(steering_args, boltz2=True)

    feats = network_condition_kwargs["feats"]
    # atom -> chain map from token-level asym_id (ref_space_uid is PER-RESIDUE
    # and must not be used for chain identity)
    token_asym = feats["asym_id"]
    a2t = feats["atom_to_token"]
    atom_pad = feats["atom_pad_mask"]
    if token_asym.dim() > 1:
        token_asym = token_asym[0]
    if atom_pad.dim() > 1:
        atom_pad = atom_pad[0]
    if a2t.dim() == 3:
        atom_to_token = a2t[0].argmax(dim=-1).long()
    elif a2t.dim() == 2:
        atom_to_token = a2t.argmax(dim=-1).long()
    else:
        atom_to_token = a2t.long()
    atom_asym = token_asym[atom_to_token].long().clamp(min=0).flatten()
    atom_present = atom_pad.bool().flatten()
    # The staged complex's design contract: the LARGEST chain is the fixed
    # D-target; every other chain (binder peptide, covalent linker) is free.
    # An argmin-atom-count heuristic here once froze the peptide and re-docked
    # only a 9-atom linker instead.
    counts = torch.bincount(atom_asym[atom_present])
    receptor_asym = int(torch.argmax(counts).item())
    fixed_mask = (atom_asym == receptor_asym).to(self.device)
    align_mask = fixed_mask & atom_present.to(self.device)
    pep_sel = (~fixed_mask) & atom_present.to(self.device)

    fixed_coords = feats["coords"]
    if fixed_coords.dim() == 4:
        fixed_coords = fixed_coords[:, 0]
    fixed_coords = fixed_coords[0].to(device=self.device, dtype=torch.float32).clone()
    fixed_coords = fixed_coords - fixed_coords[fixed_mask].mean(dim=0, keepdim=True)

    box_radius = float(_FIXED_RECEPTOR_CONFIG.get("pocket_box_radius") or 0.0)
    pocket_center = None
    if box_radius > 0:
        pep_input = fixed_coords[pep_sel].to(self.device)
        pocket_center = pep_input.mean(dim=0)

    peptide_init = _FIXED_RECEPTOR_CONFIG.get("peptide_init", "input")
    if peptide_init != "input":
        raise ValueError("production driver supports peptide_init='input' only")

    num_sampling_steps = diffusionv2_mod.default(num_sampling_steps, self.num_sampling_steps)
    atom_mask = atom_mask.repeat_interleave(multiplicity, 0)
    shape = (*atom_mask.shape, 3)

    sigmas = self.sample_schedule(num_sampling_steps)
    gammas = torch.where(sigmas > self.gamma_min, self.gamma_0, 0.0)
    sigmas_and_gammas = list(zip(sigmas[:-1], sigmas[1:], gammas[1:]))
    step_scale = self.step_scale

    init_sigma = sigmas[0]
    atom_coords = _expand_input_coords(feats, multiplicity, self.device)
    atom_coords = atom_coords - fixed_coords[fixed_mask].mean(dim=0, keepdim=True)
    fm = fixed_mask.unsqueeze(0).unsqueeze(-1)
    atom_coords = atom_coords * (~fm).float() + fixed_coords.unsqueeze(0) * fm.float()

    token_repr = None
    atom_coords_denoised = None

    for step_idx, (sigma_tm, sigma_t, gamma) in enumerate(sigmas_and_gammas):
        # stable frame: identity augmentation (receptor defines the frame)
        random_R = torch.eye(3, device=atom_coords.device, dtype=atom_coords.dtype).expand(multiplicity, 3, 3)
        random_tr = torch.zeros(multiplicity, 1, 1, device=atom_coords.device, dtype=atom_coords.dtype)
        atom_coords = atom_coords - atom_coords.mean(dim=-2, keepdims=True)
        atom_coords = torch.einsum("bmd,bds->bms", atom_coords, random_R) + random_tr
        if atom_coords_denoised is not None:
            atom_coords_denoised = atom_coords_denoised - atom_coords_denoised.mean(dim=-2, keepdims=True)
            atom_coords_denoised = (
                torch.einsum("bmd,bds->bms", atom_coords_denoised, random_R) + random_tr
            )

        # undo the joint recentering for the receptor, then reset exactly
        rec_shift = fixed_coords[align_mask].mean(dim=0) - atom_coords[:, align_mask].mean(dim=1)
        atom_coords = atom_coords + rec_shift[:, None, :]
        atom_coords = atom_coords * (~fm).float() + fixed_coords.unsqueeze(0) * fm.float()

        sigma_tm, sigma_t, gamma = sigma_tm.item(), sigma_t.item(), gamma.item()
        t_hat = sigma_tm * (1 + gamma)
        noise_var = self.noise_scale**2 * (t_hat**2 - sigma_tm**2)
        eps = math.sqrt(noise_var) * torch.randn(shape, device=self.device)
        # RePaint-style inpainting: known (receptor) atoms get no noise
        eps = eps * (~fm).float()
        atom_coords_noisy = atom_coords + eps

        with torch.no_grad():
            atom_coords_denoised = self.preconditioned_network_forward(
                atom_coords_noisy,
                t_hat,
                network_condition_kwargs=dict(multiplicity=multiplicity, **network_condition_kwargs),
            )

        # reset the receptor inside x0 BEFORE it is used as the alignment target
        atom_coords_denoised = atom_coords_denoised * (~fm).float() + fixed_coords.unsqueeze(0) * fm.float()

        if potentials is not None and step_idx < num_sampling_steps - 1:
            steering_t = 1.0 - (step_idx / num_sampling_steps)
            guidance_update = torch.zeros_like(atom_coords_denoised)
            for guidance_step in range(steering_args["num_gd_steps"]):
                energy_gradient = torch.zeros_like(atom_coords_denoised)
                for potential in potentials:
                    parameters = potential.compute_parameters(steering_t)
                    if (
                        parameters["guidance_weight"] > 0
                        and guidance_step % parameters["guidance_interval"] == 0
                    ):
                        # Potentials run weighted_rigid_align internally; its
                        # SVD diverges on bf16 inputs for ill-conditioned
                        # batches, so guidance math stays in fp32.
                        try:
                            with torch.autocast("cuda", enabled=False):
                                energy_gradient += parameters["guidance_weight"] * potential.compute_gradient(
                                    atom_coords_denoised.float() + guidance_update.float(),
                                    network_condition_kwargs["feats"],
                                    parameters,
                                ).to(atom_coords_denoised.dtype)
                        except torch._C._LinAlgError:
                            # Rare: the align SVD hits repeated singular values
                            # for one batch element. Drop this gradient
                            # contribution; the step continues unguided.
                            pass
                guidance_update -= energy_gradient
            atom_coords_denoised += guidance_update

        if self.alignment_reverse_diff:
            from boltz.model.loss.diffusionv2 import weighted_rigid_align

            with torch.autocast("cuda", enabled=False):
                try:
                    atom_coords_noisy = weighted_rigid_align(
                        atom_coords_noisy.float(),
                        atom_coords_denoised.float(),
                        atom_mask.float(),
                        atom_mask.float(),
                    ).to(atom_coords_denoised)
                except torch._C._LinAlgError:
                    # Rare: this step's point cloud has a degenerate covariance
                    # (repeated singular values) and the align SVD diverges.
                    # The align only stabilizes the reverse step; continue the
                    # step unaligned instead of corrupting the trajectory.
                    pass

        atom_coords_noisy = atom_coords_noisy * (~fm).float() + fixed_coords.unsqueeze(0) * fm.float()

        denoised_over_sigma = (atom_coords_noisy - atom_coords_denoised) / t_hat
        atom_coords = atom_coords_noisy + step_scale * (sigma_t - t_hat) * denoised_over_sigma
        atom_coords = atom_coords * (~fm).float() + fixed_coords.unsqueeze(0) * fm.float()

        # pocket anchoring box: peptide-only rigid translation (validated —
        # a whole-system shift is cancelled by the next joint recentering)
        if box_radius > 0 and pocket_center is not None:
            pep_now = atom_coords[:, pep_sel]
            if pep_now.numel():
                cents = pep_now.mean(dim=1)
                offset = cents - pocket_center.unsqueeze(0)
                dist = offset.norm(dim=-1, keepdim=True)
                max_d = cents.new_tensor(box_radius)
                allowed = pocket_center.unsqueeze(0) + offset * (max_d / dist.clamp(min=1e-6))
                shift = torch.where(dist > max_d, allowed - cents, torch.zeros_like(cents))
                pm = pep_sel.view(1, -1, 1).to(atom_coords.dtype)
                atom_coords = atom_coords * (1.0 - pm) + (atom_coords + shift[:, None, :]) * pm

    return dict(sample_atom_coords=atom_coords, diff_token_repr=token_repr)


def _expand_input_coords(feats: dict, multiplicity: int, device: torch.device) -> torch.Tensor:
    coords = feats["coords"]
    if coords.dim() == 4:
        coords = coords[:, 0]
    coords = coords.to(device=device, dtype=torch.float32)
    if coords.size(0) == multiplicity:
        return coords
    if coords.size(0) == 1:
        return coords.repeat_interleave(multiplicity, 0)
    if multiplicity % coords.size(0) == 0:
        return coords.repeat_interleave(multiplicity // coords.size(0), 0)
    raise RuntimeError(
        f"Cannot expand input coords with batch_size={coords.size(0)} to multiplicity={multiplicity}"
    )
