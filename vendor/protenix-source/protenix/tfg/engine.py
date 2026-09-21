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

"""TFG (Training-Free Guidance) engine.

This module implements the core *runtime* logic for applying TFG potentials
inside the diffusion sampler.

High-level idea
---------------
- A set of guidance "terms" (potentials) define an energy function E(x).
- We treat p(x) ~ exp(-E(x)) as a soft constraint/conditioning signal.
- During sampling, we:
  1) optionally guide x_t via gradients that flow through the denoiser
     ("denoiser-path guidance" controlled by `rho`),
  2) optionally refine the denoiser's x0 prediction directly (controlled by
     `mu`), and
  3) optionally project x0 onto constraint manifolds (projection steps).
"""

import math
import os
from typing import Any, Callable, Mapping

import torch

from protenix.tfg.config import TFGConfig, validate_features
from protenix.tfg.potentials import Potential
from protenix.utils.logger import get_logger

logger = get_logger(__name__)


def _sample_eps(
    std: float, shape: torch.Size, *, k: int, device, dtype
) -> torch.Tensor:
    """Sample Gaussian perturbations used for Monte-Carlo log-prob estimation.

    Args:
        std: Standard deviation of the noise. If 0, returns all zeros.
        shape: Base tensor shape to perturb (typically `x0.shape`).
        k: Number of Monte-Carlo samples.
        device: Target torch device.
        dtype: Target torch dtype.

    Returns:
        Tensor of shape `[k, *shape]`.
    """
    if std == 0.0:
        return torch.zeros((1, *shape), device=device, dtype=dtype)
    return std * torch.randn((k, *shape), device=device, dtype=dtype)


def _logmeanexp(x: torch.Tensor, dim: int) -> torch.Tensor:
    """Numerically-stable `log(mean(exp(x)))`.

    This is commonly used when aggregating Monte-Carlo samples in log-space.
    """
    return torch.logsumexp(x, dim=dim) - math.log(x.shape[dim])


_GRAD_LIMIT = 2.0


class TFGEngine:
    """
    TFG guidance engine.

    This class evaluates configured TFG terms and integrates them into the
    diffusion sampling step.
    """

    def __init__(self, cfg: TFGConfig, *, device: torch.device, dtype: torch.dtype):
        self.cfg = cfg
        self.device = device
        self.dtype = dtype

        # Projection ordering is fixed; pre-sort once to avoid repeated Python
        # sorting overhead inside the diffusion loop.
        self._projection_terms_sorted = sorted(
            self.cfg.terms,
            key=lambda x: (
                0
                if x.name == "ChiralAtomPotential"
                else 1
                if x.name == "PairwiseDistancePotential"
                else 2
            ),
        )

    def _energy_and_grad(
        self,
        coords: torch.Tensor,
        feats: Mapping[str, Any],
        *,
        t: float,
        step_i: int,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Compute the total energy and gradient for a coordinate tensor.

        The energy is defined by summing all *active* terms in `self.cfg.terms`.

        Args:
            coords: Atomic coordinates, shape `[*batch, N_atom, 3]`.
            feats: Feature dict used by terms (e.g. atom metadata, bonds,
                restraints, templates). This is validated once in `step()`.
            t: Diffusion "time" in [0, 1], where larger means noisier.
            step_i: 0-based diffusion step index.

        Returns:
            (energy, grad)
            - energy: shape `[*batch]`
            - grad: shape `[*batch, N_atom, 3]`
        """
        energy = torch.zeros(
            coords.shape[:-2], device=coords.device, dtype=coords.dtype
        )
        grad = torch.zeros_like(coords)
        for term in self.cfg.terms:
            if not term.active(step_i):
                continue
            # Each term returns the energy contribution and its dE/dx.
            e, g = term.energy_and_grad(coords, feats, t)
            energy = energy + e
            grad = grad + g
        return energy, grad

    def _energy_unweighted(
        self,
        coords: torch.Tensor,
        feats: Mapping[str, Any],
        *,
        t: float,
        step_i: int,
    ) -> torch.Tensor:
        """Sum of active term energies WITHOUT the guidance weight schedule.

        boltz2 separates ``resampling_weight`` (constant, drives FK particle
        selection from the first resampling event) from ``guidance_weight``
        (scheduled, drives gradient updates). Summing the SCHEDULED energy
        for resampling left early drifters unpenalised -- the pocket term
        has weight 0 for the first quarter of steps, so particles the
        denoiser prior dragged off the pocket survived every early FK event
        and by the time the weight ramped up they were 70+ A away with no
        good particle left to copy (measured: 3/8 samples in repeated runs).
        """
        total = torch.zeros(
            coords.shape[:-2], device=coords.device, dtype=torch.float32
        )
        for term in self.cfg.terms:
            if not term.active(step_i):
                continue
            total = total + term.energy_unweighted(coords, feats, t)
        return total

    def _energy(
        self,
        coords: torch.Tensor,
        feats: Mapping[str, Any],
        *,
        t: float,
        step_i: int,
    ) -> torch.Tensor:
        """Compute total energy only (no explicit analytic gradients).

        This is useful for denoiser-path guidance where we rely on autograd to
        backprop through the denoiser and only need a scalar objective.
        """
        energy = torch.zeros(
            coords.shape[:-2], device=coords.device, dtype=coords.dtype
        )
        for term in self.cfg.terms:
            if not term.active(step_i):
                continue
            energy = energy + term.energy(coords, feats, t)
        return energy

    def _logp_x0(
        self,
        x0: torch.Tensor,
        eps: torch.Tensor,
        feats: Mapping[str, Any],
        *,
        t: float,
        step_i: int,
    ) -> torch.Tensor:
        """Estimate `log p(x0)` via Monte-Carlo samples (energy-only path)."""
        k = eps.shape[0]
        x0_eps = (x0.unsqueeze(0) + eps).reshape(-1, *x0.shape[-2:])
        e = self._energy(x0_eps, feats, t=t, step_i=step_i)
        e = e.reshape(k, *x0.shape[:-2])
        logp = -e
        if k == 1:
            return logp.squeeze(0)
        return _logmeanexp(logp, dim=0)

    def _logp_and_grad_x0(
        self,
        x0: torch.Tensor,
        eps: torch.Tensor,
        feats: Mapping[str, Any],
        *,
        t: float,
        step_i: int,
        log_components: bool = False,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Estimate `log p(x0)` and `d/dx0 log p(x0)` via Monte-Carlo samples.

        We interpret the configured TFG terms as an unnormalized density:

            p(x0) ~ exp(-E(x0))

        and compute a Monte-Carlo approximation of `log p(x0)` under additive
        perturbations `eps` (reparameterization used for stability / smoothing).

        Args:
            x0: Coordinates to evaluate, shape `[*batch, N_atom, 3]`.
            eps: Noise samples, shape `[K, *batch, N_atom, 3]`.
            feats: Feature dict consumed by the energy terms.
            t: Diffusion "time" in [0, 1].
            step_i: 0-based diffusion step index.
            log_components: If True, logs per-term energies for debugging at the
                final iteration.

        Returns:
            (avg_logp, grad_x0)
            - avg_logp: shape `[*batch]`, approximating log-mean-exp over K.
            - grad_x0: shape `[*batch, N_atom, 3]`.
        """

        k = eps.shape[0]
        # Flatten the MC dimension into the batch dimension so each term can
        # run in a single forward pass.
        # [K, *batch, N_atom, 3] -> [K*batch, N_atom, 3]
        x0_eps = (x0.unsqueeze(0) + eps).reshape(-1, *x0.shape[-2:])

        e, g = self._energy_and_grad(x0_eps, feats, t=t, step_i=step_i)
        # Reshape back to `[K, *batch]` and `[K, *batch, N_atom, 3]`.
        e = e.reshape(k, *x0.shape[:-2])
        g = g.reshape(k, *x0.shape)

        logp = -e  # [K, *batch]
        if k == 1:
            avg_logp = logp.squeeze(0)
            # Since logp = -E, d/dx logp = - dE/dx.
            grad = (-g).squeeze(0)
        else:
            # Aggregate over MC samples in log-space.
            avg_logp = _logmeanexp(logp, dim=0)  # [*batch]

            # Importance weights over eps samples.
            # Note: exp(logp - logmeanexp(logp)) renormalizes to softmax(logp).
            w = torch.softmax(logp, dim=0)  # [K, *batch]

            # Since logp = -E, d/dx logp = - dE/dx.
            dlogp = -g
            # Weighted average gradient across the MC batch.
            grad = (w.unsqueeze(-1).unsqueeze(-1) * dlogp).sum(dim=0)

        if log_components:
            # only for debugging, compute per-term energies on x0 (no eps)
            with torch.no_grad():
                for term in self.cfg.terms:
                    if not term.active(step_i):
                        continue
                    e_t = term.energy(x0, feats, t)
                    logger.info(
                        f"TFG last-step energy {term.name}: {e_t.detach().cpu().tolist()}"
                    )

        return avg_logp, grad

    def _project(
        self, coords: torch.Tensor, feats: Mapping[str, Any], *, t: float, step_i: int
    ) -> torch.Tensor:
        """Run iterative projection steps for projection-enabled terms.

        Projection is treated as an additive coordinate update `delta`.
        Each projection-enabled term may propose a small correction, and we
        accumulate these corrections for `projection_steps` iterations.

        Args:
            coords: Coordinates to project, shape `[*batch, N_atom, 3]`.
            feats: Feature dict consumed by projection terms.
            t: Diffusion "time" in [0, 1].
            step_i: 0-based diffusion step index.

        Returns:
            A delta tensor with the same shape as `coords`.
        """
        if (
            (not self.cfg.enable)
            or self.cfg.projection_outer_steps <= 0
            or self.cfg.projection_inner_steps <= 0
        ):
            return torch.zeros_like(coords)

        delta = torch.zeros_like(coords)

        # Only keep terms that actually implement a non-trivial projection.
        # Most potentials don't override `Potential._project` (no-op), and calling
        # them inside the nested projection loops is wasted compute.
        sorted_terms = [
            term
            for term in self._projection_terms_sorted
            if (type(term._potential)._project is not Potential._project)
        ]

        for _ in range(self.cfg.projection_outer_steps):
            for term in sorted_terms:
                if (not term.active(step_i)) or (not term.enable_projection):
                    continue
                for _ in range(self.cfg.projection_inner_steps):
                    d = term.project(coords + delta, feats, t)
                    if d is not None:
                        delta = delta + d
        return delta

    def project(
        self,
        coords: torch.Tensor,
        feats: Mapping[str, Any],
        *,
        step_i: int,
        num_diffusion_steps: int,
    ) -> torch.Tensor:
        """Public projection hook.

        This is used when the caller wants to apply projections without running
        the full TFG-aware diffusion update.

        Args:
            coords: Coordinates, shape `[*batch, N_atom, 3]`.
            feats: Feature dict.
            step_i: 0-based diffusion step index.
            num_diffusion_steps: Total number of diffusion steps.

        Returns:
            Projection delta with the same shape as `coords`.
        """
        # Map the discrete step index to a continuous time in [0, 1].
        t = 1.0 - float(step_i) / float(max(1, num_diffusion_steps))
        return self._project(coords, feats, t=t, step_i=step_i)

    def step(
        self,
        denoise_net: Callable,
        *,
        x: torch.Tensor,
        t_hat: torch.Tensor,
        c_tau: torch.Tensor,
        step_scale_eta: float,
        step_i: int,
        num_diffusion_steps: int,
        input_feature_dict: Mapping[str, Any],
        s_inputs: torch.Tensor,
        s_trunk: torch.Tensor,
        z_trunk: torch.Tensor,
        pair_z: torch.Tensor,
        p_lm: torch.Tensor,
        c_l: torch.Tensor,
        chunk_size: int | None,
        inplace_safe: bool,
        enable_efficient_fusion: bool,
    ) -> torch.Tensor:
        """Run one TFG-aware diffusion update: `x_t -> x_{t-1}`.

        The update combines:
        - denoiser-path guidance (controlled by `cfg.rho`),
        - direct refinement on the predicted x0 (controlled by `cfg.mu`),
        - optional constraint projection (controlled by `cfg.projection_steps`),
        - and a predictor-corrector style diffusion update with stochasticity.

        Args:
            denoise_net: Callable denoiser that maps `(x_t, t_hat, feats, ...)`
                to a predicted `x0`.
            x: Current noisy coordinates `x_t`, shape `[*batch, N_atom, 3]`.
            t_hat: Current noise level (scalar per batch), shape `[*batch]`.
            c_tau: Next noise level (scalar per batch), shape `[*batch]`.
            step_scale_eta: Global step size used by the sampler.
            step_i: 0-based diffusion step index.
            num_diffusion_steps: Total number of diffusion steps.
            input_feature_dict: Feature dict shared by denoiser and TFG terms.
            s_inputs/s_trunk/z_trunk/pair_z/p_lm/c_l: Model-specific states
                forwarded to `denoise_net`.
            chunk_size: Optional chunk size to reduce memory use.
            inplace_safe: Whether `denoise_net` may do in-place ops.
            enable_efficient_fusion: Whether to enable fused kernels.

        Returns:
            Updated noisy coordinates `x_{t-1}` with shape `[*batch, N_atom, 3]`.
        """
        if step_i == 0:
            # Fail fast: terms may require specific features (e.g. bonds,
            # chirality annotations, distance restraints).
            validate_features(input_feature_dict, self.cfg.terms)
        _nan_debug = bool(os.environ.get("PROTENIX_TFG_DEBUG_NAN", ""))

        def _nan_report(tag: str, tensor: torch.Tensor) -> None:
            if _nan_debug and tensor is not None and not bool(
                torch.isfinite(tensor).all()
            ):
                bad = int((~torch.isfinite(tensor)).sum().item())
                logger.warning(
                    f"TFG-NAN {tag} step={step_i} nonfinite={bad}/{tensor.numel()}"
                )

        # TFG potentials mix fp32 intermediates (linalg solves, scatter_add_)
        # with the coordinate dtype; under PROTENIX_LOW_VRAM the sampler runs
        # bf16 and e.g. ChiralAtomPotential's projection then fails with a
        # scatter dtype mismatch. Run all guidance math in fp32 and only the
        # denoiser calls (whose modules own the model dtype) in orig_dtype.
        orig_dtype = x.dtype
        x = x.float()
        t_hat = t_hat.float()
        c_tau = c_tau.float()

        # A normalized time used by energy terms. This does not have to match
        # the sampler's `t_hat` schedule exactly; it is a convenient [0, 1]
        # parameter (1 = early/noisy, 0 = late/clean).
        t = 1.0 - float(step_i) / float(max(1, num_diffusion_steps))
        eps = _sample_eps(
            self.cfg.eps_std,
            x.shape,
            k=self.cfg.eps_batch,
            device=self.device,
            dtype=self.dtype,
        )

        x_work = x
        for outer in range(self.cfg.outer_steps):
            # 1) guidance on x_t through the denoiser path
            if self.cfg.rho != 0.0:
                with torch.enable_grad():
                    x_var = x_work.detach().requires_grad_(True)
                    x0_pred = denoise_net(
                        x_noisy=x_var.to(orig_dtype),
                        t_hat_noise_level=t_hat.to(orig_dtype),
                        input_feature_dict=input_feature_dict,
                        s_inputs=s_inputs,
                        s_trunk=s_trunk,
                        z_trunk=z_trunk,
                        pair_z=pair_z,
                        p_lm=p_lm,
                        c_l=c_l,
                        chunk_size=chunk_size,
                        inplace_safe=inplace_safe,
                        enable_efficient_fusion=enable_efficient_fusion,
                    )
                    # Estimate log p(x0_pred) (energy-only) and backprop through
                    # the denoiser to get guidance gradient on x_t.
                    avg_logp = self._logp_x0(
                        x0_pred.float(),
                        eps,
                        input_feature_dict,
                        t=t,
                        step_i=step_i,
                    )
                    grad_xt = torch.autograd.grad(
                        avg_logp.sum(),
                        x_var,
                        retain_graph=False,
                        create_graph=False,
                        allow_unused=True,
                    )[0]
                    if grad_xt is None:
                        grad_xt = torch.zeros_like(x_var)
                    _nan_report("grad_xt", grad_xt)
                    # Degenerate local geometry (e.g. near-collinear torsion
                    # quadruples after a noise step) can blow an individual
                    # guidance gradient to inf/NaN; one bad vector would
                    # poison every later coordinate. Bound the guidance step
                    # and drop non-finite components for this step only.
                    grad_xt = torch.nan_to_num(
                        grad_xt, nan=0.0, posinf=_GRAD_LIMIT, neginf=-_GRAD_LIMIT
                    ).clamp(min=-_GRAD_LIMIT, max=_GRAD_LIMIT)
                    _nan_report("x0_pred", x0_pred)
                xt_shift = grad_xt.detach() * float(self.cfg.rho)
            else:
                xt_shift = torch.zeros_like(x_work)

            # 2) denoise with the x_t shift applied
            with torch.no_grad():
                # The denoiser is treated as a black box in this branch.
                x0 = denoise_net(
                    x_noisy=(x_work + xt_shift).to(orig_dtype),
                    t_hat_noise_level=t_hat.to(orig_dtype),
                    input_feature_dict=input_feature_dict,
                    s_inputs=s_inputs,
                    s_trunk=s_trunk,
                    z_trunk=z_trunk,
                    pair_z=pair_z,
                    p_lm=p_lm,
                    c_l=c_l,
                    chunk_size=chunk_size,
                    inplace_safe=inplace_safe,
                    enable_efficient_fusion=enable_efficient_fusion,
                ).float()
            _nan_report("x0_denoise", x0)

            # 3) projection
            x0_ref = x0.detach()
            x0_ref = x0_ref + self._project(
                x0_ref, input_feature_dict, t=t, step_i=step_i
            )
            _nan_report("x0_projected", x0_ref)

            # 4) refinement directly on x0
            for inner in range(self.cfg.inner_steps):
                if self.cfg.mu == 0.0:
                    break
                # Optional logging at the very last refinement step.
                log_components = (
                    self.cfg.log_last_step_energy
                    and (step_i == num_diffusion_steps - 1)
                    and (outer == self.cfg.outer_steps - 1)
                    and (inner == self.cfg.inner_steps - 1)
                )
                _, grad_x0 = self._logp_and_grad_x0(
                    x0_ref,
                    eps,
                    input_feature_dict,
                    t=t,
                    step_i=step_i,
                    log_components=log_components,
                )
                # Gradient ascent on log p(x0) (equivalently, gradient descent
                # on energy E). The step size is `cfg.mu`.
                grad_x0 = torch.nan_to_num(
                    grad_x0, nan=0.0, posinf=_GRAD_LIMIT, neginf=-_GRAD_LIMIT
                ).clamp(min=-_GRAD_LIMIT, max=_GRAD_LIMIT)
                # Aromatic chi-subspace projection: re-express the
                # gradient on each aromatic side chain (rows CA,CB,CG,
                # side...) as a pure chi1/chi2 rotation -- the least-
                # squares fit of the per-atom gradients onto the two
                # torsion velocity fields. The steric push then TURNS the
                # side chain out of the receptor instead of deforming the
                # ring: per-atom gradients on ring atoms measured boat
                # buckling and junction-angle damage at every weight
                # tried (T5: w0.6 -> P N-CA-CB 146 deg; T6: w0.15 ->
                # CB-CG-CD1 141 deg), while the untouched sampler keeps
                # ring planes at 0.000-0.008 A.
                dofs = input_feature_dict.get("aro_dof")
                if dofs is not None and dofs.numel() > 0:
                    with torch.autocast("cuda", enabled=False):
                        lead = grad_x0.shape[:-2]
                        n_atom = grad_x0.shape[-2]
                        g32 = grad_x0.float().reshape(-1, n_atom, 3)
                        x32 = x0_ref.float().reshape(-1, n_atom, 3)
                        for gi in range(dofs.shape[0]):
                            rows = dofs[gi]
                            rows = rows[rows >= 0]
                            if rows.numel() < 4:
                                continue
                            ca, cb, cg = rows[0], rows[1], rows[2]
                            ring = rows[3:]
                            u1 = x32[:, cb] - x32[:, ca]
                            u1 = u1 / u1.norm(dim=-1, keepdim=True).clamp(min=1e-8)
                            u2 = x32[:, cg] - x32[:, cb]
                            u2 = u2 / u2.norm(dim=-1, keepdim=True).clamp(min=1e-8)
                            # torsion velocity fields, EXPLICITLY expanded to
                            # identical shapes (torch.cross broadcasting
                            # [F,1,3]x[F,m,3] silently made [F,m,3] values
                            # for the [F,3] CG slot -- measured crash)
                            m = int(ring.numel())
                            u1e = u1[:, None, :].expand(-1, m, 3)
                            u2e = u2[:, None, :].expand(-1, m, 3)
                            e1 = torch.linalg.cross(
                                u1e, x32[:, ring] - x32[:, ca, None, :], dim=-1)
                            e2 = torch.linalg.cross(
                                u2e, x32[:, ring] - x32[:, cb, None, :], dim=-1)
                            gr = g32[:, ring]
                            a11 = (e1 * e1).sum(dim=(1, 2))
                            a12 = (e1 * e2).sum(dim=(1, 2))
                            a22 = (e2 * e2).sum(dim=(1, 2))
                            b1 = (e1 * gr).sum(dim=(1, 2))
                            b2 = (e2 * gr).sum(dim=(1, 2))
                            det = a11 * a22 - a12 * a12
                            det = torch.where(
                                det.abs() < 1e-12, torch.ones_like(det), det)
                            chi1 = ((a22 * b1 - a12 * b2) / det)[:, None, None]
                            chi2 = ((a11 * b2 - a12 * b1) / det)[:, None, None]
                            # CB lies on the chi1 axis (zero velocity);
                            # CG lies on the chi2 axis (chi1 only)
                            g32[:, cb] = 0.0
                            g32[:, cg] = torch.linalg.cross(
                                u1, x32[:, cg] - x32[:, ca], dim=-1) \
                                * chi1[:, 0]
                            g32[:, ring] = e1 * chi1 + e2 * chi2
                        grad_x0 = g32.reshape(grad_x0.shape).to(grad_x0.dtype)
                x0_ref = x0_ref + grad_x0 * float(self.cfg.mu)

            # one-shot forensic dump: the refined x0 vs the state that
            # leaves the sampler (three independent mysteries -- final
            # clash, guard-era geometry, stereochemistry -- all showed the
            # WRITTEN structure deviating from the energy-clean x0_ref)
            _dump = os.environ.get("PROTENIX_DUMP_X0", "").strip()
            if (_dump
                    and step_i == num_diffusion_steps - 1
                    and outer == self.cfg.outer_steps - 1):
                torch.save(
                    {"x0_ref": x0_ref.detach().cpu(),
                     "x_work": x_work.detach().cpu(),
                     "eta": float(step_scale_eta), "t_hat": float(t_hat.reshape(-1)[0]),
                     "c_tau": float(c_tau), "step_i": int(step_i)},
                    f"{_dump}/x0_step{step_i}_chunk{outer}.pt")

            # 4.5) SE(3) drift correction (boltz-2 alignment_reverse_diff):
            # the sampler's per-step random augmentation re-frames the whole
            # system every step, and the Euler extrapolation can accumulate a
            # global drift between the noisy state and the denoiser's own
            # prediction. Rigid-aligning x_work onto x0_ref removes exactly
            # that drift (a global SE(3) move, internal geometry untouched)
            # so the update direction measures internal deformation only.
            # boltz-2 ships this enabled by default in inference.
            if self.cfg.align_to_x0:
                from protenix.metrics.rmsd import weighted_rigid_align
                with torch.autocast("cuda", enabled=False):
                    n_atom = x_work.shape[-2]
                    _w = torch.ones(n_atom, device=x_work.device,
                                    dtype=torch.float32)
                    x_work = weighted_rigid_align(
                        x_work.float(), x0_ref.float(), _w,
                    ).to(x_work.dtype)

            # 5) predictor-corrector update
            # keep sign convention consistent with AF3 sampler
            # `direction` is the normalized update direction implied by x0.
            direction = (x_work + xt_shift - x0_ref) / t_hat[..., None, None]
            dt = c_tau - t_hat
            x_next = (
                x_work
                + xt_shift
                + float(step_scale_eta) * dt[..., None, None] * direction
            )

            # stochasticity
            # Inject noise so the marginal at the next noise level matches the
            # chosen diffusion schedule.
            sigma = torch.sqrt(t_hat**2 - c_tau**2)
            x_work = x_next + sigma[..., None, None] * torch.randn_like(x_next)
            _nan_report("x_next", x_next)
            _nan_report("xt_shift", xt_shift)
            _nan_report("x0_ref_final", x0_ref)

        return x_next.to(orig_dtype)
