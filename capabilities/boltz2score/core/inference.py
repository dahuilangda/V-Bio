#!/usr/bin/env python3
"""Run Boltz2 confidence inference with optional structure refinement."""

from __future__ import annotations

import argparse
import json
import os
import warnings
from dataclasses import asdict
from math import sqrt
from pathlib import Path
from types import MethodType
from typing import Literal, Optional

import numpy as np
import torch
import torch.nn.functional as F  # noqa: N812
from pytorch_lightning import Trainer, seed_everything
from pytorch_lightning.strategies import DDPStrategy
from torch import Tensor

import boltz.model.modules.diffusionv2 as diffusionv2_mod
from boltz.data import const
from boltz.data.module.inferencev2 import Boltz2InferenceDataModule
from boltz.data.types import Manifest, Record, StructureV2
from boltz.data.write.writer import BoltzWriter
from boltz.main import (
    Boltz2DiffusionParams,
    BoltzSteeringParams,
    MSAModuleArgs,
    PairformerArgsV2,
    get_cache_path,
)
from boltz.model.loss.diffusionv2 import weighted_rigid_align
from boltz.model.models.boltz2 import Boltz2
from boltz.model.potentials.potentials import get_potentials


def _expand_input_coords_for_sampling(
    feats: dict,
    multiplicity: int,
    device: torch.device,
) -> torch.Tensor:
    """Repeat input coordinates to match Boltz2 diffusion multiplicity."""
    coords = feats["coords"]
    if coords.dim() == 4:
        coords = coords[:, 0]
    if coords.dim() != 3:
        raise RuntimeError(
            f"Expected feats['coords'] to have shape (batch, atoms, 3) or (batch, 1, atoms, 3), got {tuple(coords.shape)}."
        )
    coords = coords.to(device=device, dtype=torch.float32)
    batch_size = coords.size(0)
    if batch_size == multiplicity:
        return coords
    if batch_size == 1:
        return coords.repeat_interleave(multiplicity, 0)
    if multiplicity % batch_size != 0:
        raise RuntimeError(
            f"Cannot expand input coords with batch_size={batch_size} to multiplicity={multiplicity}."
        )
    return coords.repeat_interleave(multiplicity // batch_size, 0)


def _sample_with_optional_input_init(
    self,
    atom_mask,
    num_sampling_steps=None,
    multiplicity=1,
    max_parallel_samples=None,
    steering_args=None,
    **network_condition_kwargs,
):
    """Boltz2 inference sampler with optional input-pose initialization."""
    if steering_args is not None and (
        steering_args["fk_steering"]
        or steering_args["physical_guidance_update"]
        or steering_args["contact_guidance_update"]
    ):
        potentials = get_potentials(steering_args, boltz2=True)

    if steering_args["fk_steering"]:
        multiplicity = multiplicity * steering_args["num_particles"]
        energy_traj = torch.empty((multiplicity, 0), device=self.device)
        resample_weights = torch.ones(multiplicity, device=self.device).reshape(
            -1, steering_args["num_particles"]
        )
    if (
        steering_args["physical_guidance_update"]
        or steering_args["contact_guidance_update"]
    ):
        scaled_guidance_update = torch.zeros(
            (multiplicity, *atom_mask.shape[1:], 3),
            dtype=torch.float32,
            device=self.device,
        )
    if max_parallel_samples is None:
        max_parallel_samples = multiplicity

    num_sampling_steps = diffusionv2_mod.default(num_sampling_steps, self.num_sampling_steps)
    atom_mask = atom_mask.repeat_interleave(multiplicity, 0)

    shape = (*atom_mask.shape, 3)

    sigmas = self.sample_schedule(num_sampling_steps)
    gammas = torch.where(sigmas > self.gamma_min, self.gamma_0, 0.0)
    sigmas_and_gammas = list(zip(sigmas[:-1], sigmas[1:], gammas[1:]))
    if self.training and self.step_scale_random is not None:
        step_scale = np.random.choice(self.step_scale_random)
    else:
        step_scale = self.step_scale

    init_sigma = sigmas[0]
    init_source = str(getattr(self, "_boltz2score_sampling_init_source", "noise")).strip().lower()
    if init_source == "input":
        atom_coords = _expand_input_coords_for_sampling(
            network_condition_kwargs["feats"],
            multiplicity=multiplicity,
            device=self.device,
        )
        input_noise_scale = float(getattr(self, "_boltz2score_input_init_noise_scale", 0.0))
        if input_noise_scale > 0:
            atom_coords = atom_coords + (
                init_sigma * input_noise_scale * torch.randn_like(atom_coords) * atom_mask.unsqueeze(-1)
            )
    else:
        atom_coords = init_sigma * torch.randn(shape, device=self.device)
    token_repr = None
    atom_coords_denoised = None

    for step_idx, (sigma_tm, sigma_t, gamma) in enumerate(sigmas_and_gammas):
        random_R, random_tr = diffusionv2_mod.compute_random_augmentation(
            multiplicity, device=atom_coords.device, dtype=atom_coords.dtype
        )
        atom_coords = atom_coords - atom_coords.mean(dim=-2, keepdims=True)
        atom_coords = torch.einsum("bmd,bds->bms", atom_coords, random_R) + random_tr
        if atom_coords_denoised is not None:
            atom_coords_denoised -= atom_coords_denoised.mean(dim=-2, keepdims=True)
            atom_coords_denoised = (
                torch.einsum("bmd,bds->bms", atom_coords_denoised, random_R) + random_tr
            )
        if (
            steering_args["physical_guidance_update"]
            or steering_args["contact_guidance_update"]
        ) and scaled_guidance_update is not None:
            scaled_guidance_update = torch.einsum(
                "bmd,bds->bms", scaled_guidance_update, random_R
            )

        sigma_tm, sigma_t, gamma = sigma_tm.item(), sigma_t.item(), gamma.item()

        t_hat = sigma_tm * (1 + gamma)
        steering_t = 1.0 - (step_idx / num_sampling_steps)
        noise_var = self.noise_scale**2 * (t_hat**2 - sigma_tm**2)
        eps = sqrt(noise_var) * torch.randn(shape, device=self.device)
        atom_coords_noisy = atom_coords + eps

        with torch.no_grad():
            atom_coords_denoised = torch.zeros_like(atom_coords_noisy)
            sample_ids = torch.arange(multiplicity).to(atom_coords_noisy.device)
            sample_ids_chunks = sample_ids.chunk(multiplicity % max_parallel_samples + 1)

            for sample_ids_chunk in sample_ids_chunks:
                atom_coords_denoised_chunk = self.preconditioned_network_forward(
                    atom_coords_noisy[sample_ids_chunk],
                    t_hat,
                    network_condition_kwargs=dict(
                        multiplicity=sample_ids_chunk.numel(),
                        **network_condition_kwargs,
                    ),
                )
                atom_coords_denoised[sample_ids_chunk] = atom_coords_denoised_chunk

            if steering_args["fk_steering"] and (
                (
                    step_idx % steering_args["fk_resampling_interval"] == 0
                    and noise_var > 0
                )
                or step_idx == num_sampling_steps - 1
            ):
                energy = torch.zeros(multiplicity, device=self.device)
                for potential in potentials:
                    parameters = potential.compute_parameters(steering_t)
                    if parameters["resampling_weight"] > 0:
                        component_energy = potential.compute(
                            atom_coords_denoised,
                            network_condition_kwargs["feats"],
                            parameters,
                        )
                        energy += parameters["resampling_weight"] * component_energy
                energy_traj = torch.cat((energy_traj, energy.unsqueeze(1)), dim=1)

                if step_idx == 0 or energy_traj.shape[1] < 2:
                    log_G = -1 * energy
                else:
                    log_G = energy_traj[:, -2] - energy_traj[:, -1]

                if (
                    steering_args["physical_guidance_update"]
                    or steering_args["contact_guidance_update"]
                ) and noise_var > 0:
                    ll_difference = (
                        eps**2 - (eps + scaled_guidance_update) ** 2
                    ).sum(dim=(-1, -2)) / (2 * noise_var)
                else:
                    ll_difference = torch.zeros_like(energy)

                resample_weights = F.softmax(
                    (ll_difference + steering_args["fk_lambda"] * log_G).reshape(
                        -1, steering_args["num_particles"]
                    ),
                    dim=1,
                )

            if (
                steering_args["physical_guidance_update"]
                or steering_args["contact_guidance_update"]
            ) and step_idx < num_sampling_steps - 1:
                guidance_update = torch.zeros_like(atom_coords_denoised)
                for guidance_step in range(steering_args["num_gd_steps"]):
                    energy_gradient = torch.zeros_like(atom_coords_denoised)
                    for potential in potentials:
                        parameters = potential.compute_parameters(steering_t)
                        if (
                            parameters["guidance_weight"] > 0
                            and (guidance_step) % parameters["guidance_interval"] == 0
                        ):
                            energy_gradient += parameters[
                                "guidance_weight"
                            ] * potential.compute_gradient(
                                atom_coords_denoised + guidance_update,
                                network_condition_kwargs["feats"],
                                parameters,
                            )
                    guidance_update -= energy_gradient
                atom_coords_denoised += guidance_update
                scaled_guidance_update = (
                    guidance_update
                    * -1
                    * self.step_scale
                    * (sigma_t - t_hat)
                    / t_hat
                )

            if steering_args["fk_steering"] and (
                (
                    step_idx % steering_args["fk_resampling_interval"] == 0
                    and noise_var > 0
                )
                or step_idx == num_sampling_steps - 1
            ):
                resample_indices = (
                    torch.multinomial(
                        resample_weights,
                        resample_weights.shape[1] if step_idx < num_sampling_steps - 1 else 1,
                        replacement=True,
                    )
                    + resample_weights.shape[1]
                    * torch.arange(
                        resample_weights.shape[0], device=resample_weights.device
                    ).unsqueeze(-1)
                ).flatten()

                atom_coords = atom_coords[resample_indices]
                atom_coords_noisy = atom_coords_noisy[resample_indices]
                atom_mask = atom_mask[resample_indices]
                if atom_coords_denoised is not None:
                    atom_coords_denoised = atom_coords_denoised[resample_indices]
                energy_traj = energy_traj[resample_indices]
                if (
                    steering_args["physical_guidance_update"]
                    or steering_args["contact_guidance_update"]
                ):
                    scaled_guidance_update = scaled_guidance_update[resample_indices]
                if token_repr is not None:
                    token_repr = token_repr[resample_indices]

        if self.alignment_reverse_diff:
            with torch.autocast("cuda", enabled=False):
                atom_coords_noisy = weighted_rigid_align(
                    atom_coords_noisy.float(),
                    atom_coords_denoised.float(),
                    atom_mask.float(),
                    atom_mask.float(),
                )

            atom_coords_noisy = atom_coords_noisy.to(atom_coords_denoised)

        denoised_over_sigma = (atom_coords_noisy - atom_coords_denoised) / t_hat
        atom_coords_next = atom_coords_noisy + step_scale * (sigma_t - t_hat) * denoised_over_sigma
        atom_coords = atom_coords_next

    return dict(sample_atom_coords=atom_coords, diff_token_repr=token_repr)


class Boltz2ScoreModel(Boltz2):
    """Boltz2 model with an explicit coordinate output policy."""

    def _configure_structure_sampling(self) -> None:
        if not hasattr(self, "structure_module"):
            return
        sample_fn = getattr(self.structure_module, "sample", None)
        if sample_fn is None:
            return
        if not getattr(self.structure_module, "_boltz2score_input_init_patch", False):
            self.structure_module.sample = MethodType(
                _sample_with_optional_input_init,
                self.structure_module,
            )
            self.structure_module._boltz2score_input_init_patch = True
        self.structure_module._boltz2score_sampling_init_source = str(
            self.predict_args.get("sampling_init_source", "noise")
        ).strip().lower()
        self.structure_module._boltz2score_input_init_noise_scale = float(
            self.predict_args.get("input_init_noise_scale", 0.0)
        )

    def _coords_from_input_batch(self, batch: dict) -> torch.Tensor:
        """Build prediction coordinates directly from input structure coordinates."""
        coords = batch["coords"]
        if coords.dim() == 4:
            # (B, S, L, 3) -> use first structure sample per batch item.
            coords = coords[:, 0]
        if coords.dim() == 3:
            if coords.size(0) != 1:
                raise RuntimeError(
                    "Input-coordinate mode only supports batch size 1 for predict; "
                    f"got coords shape {tuple(coords.shape)}."
                )
            coords = coords[0]
        if coords.dim() != 2 or coords.size(-1) != 3:
            raise RuntimeError(
                f"Unexpected input coords shape {tuple(coords.shape)} in input-coordinate mode."
            )
        num_samples = int(self.predict_args.get("diffusion_samples", 1))
        return coords.unsqueeze(0).repeat(num_samples, 1, 1)

    def _coords_from_structure_samples(self, out: dict) -> torch.Tensor:
        """Build prediction coordinates from diffusion/refinement samples."""
        coords = out.get("sample_atom_coords")
        if coords is None:
            raise RuntimeError(
                "Expected 'sample_atom_coords' in model output when structure refinement is enabled."
            )
        return coords

    def _single_input_coords(self, batch: dict) -> torch.Tensor:
        """Return a single centered input coordinate set with shape (atoms, 3)."""
        coords = batch["coords"]
        if coords.dim() == 4:
            coords = coords[:, 0]
        if coords.dim() == 3:
            if coords.size(0) != 1:
                raise RuntimeError(
                    "Input-coordinate mode only supports batch size 1 for predict; "
                    f"got coords shape {tuple(coords.shape)}."
                )
            coords = coords[0]
        if coords.dim() != 2 or coords.size(-1) != 3:
            raise RuntimeError(
                f"Unexpected input coords shape {tuple(coords.shape)} in input-coordinate mode."
            )
        return coords

    def _resolve_coords(self, batch: dict, out: dict) -> torch.Tensor:
        source = str(self.predict_args.get("coordinate_source", "sample")).strip().lower()
        if source == "sample":
            return self._coords_from_structure_samples(out)
        if source == "input":
            return self._coords_from_input_batch(batch)
        raise ValueError(
            f"Unsupported coordinate_source={source!r}. Expected 'sample' or 'input'."
        )

    def _maybe_override_reference_positions(self, batch: dict) -> dict:
        reference_source = str(self.predict_args.get("reference_source", "default")).strip().lower()
        if reference_source != "input":
            return batch
        input_coords = self._single_input_coords(batch)
        new_batch = dict(batch)
        new_batch["ref_pos"] = input_coords.unsqueeze(0).to(batch["ref_pos"])
        return new_batch

    def predict_step(self, batch, batch_idx: int, dataloader_idx: int = 0) -> dict:
        batch = self._maybe_override_reference_positions(batch)
        self._configure_structure_sampling()
        out = self(
            batch,
            recycling_steps=self.predict_args["recycling_steps"],
            num_sampling_steps=self.predict_args["sampling_steps"],
            diffusion_samples=self.predict_args["diffusion_samples"],
            max_parallel_samples=self.predict_args["max_parallel_samples"],
            run_confidence_sequentially=True,
        )

        pred_dict = {"exception": False}
        pred_dict["masks"] = batch["atom_pad_mask"]
        pred_dict["token_masks"] = batch["token_pad_mask"]
        pred_dict["s"] = out["s"]
        pred_dict["z"] = out["z"]
        pred_dict["coords"] = self._resolve_coords(batch, out)

        if self.confidence_prediction:
            for key in (
                "pde",
                "plddt",
                "complex_plddt",
                "complex_iplddt",
                "complex_pde",
                "complex_ipde",
                "pae",
                "ptm",
                "iptm",
                "ligand_iptm",
                "protein_iptm",
                "pair_chains_iptm",
            ):
                if key in out:
                    pred_dict[key] = out[key]

            if "complex_plddt" in out and ("ptm" in out or "iptm" in out):
                iptm = out.get("iptm")
                ptm = out.get("ptm")
                if iptm is not None and not torch.allclose(iptm, torch.zeros_like(iptm)):
                    iptm_or_ptm = iptm
                elif ptm is not None:
                    iptm_or_ptm = ptm
                else:
                    iptm_or_ptm = None
                if iptm_or_ptm is not None:
                    pred_dict["confidence_score"] = (
                        4 * out["complex_plddt"] + iptm_or_ptm
                    ) / 5

        return pred_dict


class Boltz2ScoreWriter(BoltzWriter):
    """Boltz writer with raw ligand-atom pLDDT dumps for debugging."""

    def _raw_ligand_plddt_entries(
        self,
        structure: StructureV2,
        plddts: Tensor | None,
    ) -> list[dict[str, object]]:
        if plddts is None:
            return []

        values = plddts.detach().cpu().reshape(-1)
        entries: list[dict[str, object]] = []
        res_num = 0
        prev_polymer_resnum = -1
        ligand_index_offset = 0

        for chain in structure.chains:
            is_ligand_chain = int(chain["mol_type"]) == const.chain_type_ids["NONPOLYMER"]
            chain_name = str(chain["name"])
            res_start = int(chain["res_idx"])
            res_end = res_start + int(chain["res_num"])
            residues = structure.residues[res_start:res_end]

            for residue in residues:
                atom_start = int(residue["atom_idx"])
                atom_end = atom_start + int(residue["atom_num"])
                atoms = structure.atoms[atom_start:atom_end]
                residue_name = str(residue["name"])
                residue_index = int(residue["res_idx"]) + 1

                for atom in atoms:
                    if not atom["is_present"]:
                        continue

                    if not is_ligand_chain:
                        token_index = res_num + ligand_index_offset
                        prev_polymer_resnum = res_num
                    else:
                        ligand_index_offset += 1
                        token_index = prev_polymer_resnum + ligand_index_offset

                    if token_index < 0 or token_index >= values.numel():
                        continue
                    if not is_ligand_chain:
                        continue

                    atom_name = str(atom["name"])
                    plddt_norm = float(values[token_index].item())
                    entries.append(
                        {
                            "chain": chain_name,
                            "res_name": residue_name,
                            "res_idx": residue_index,
                            "atom_name": atom_name,
                            "writer_token_index": int(token_index),
                            "plddt_normalized": plddt_norm,
                            "plddt": 100.0 * plddt_norm,
                        }
                    )

                if not is_ligand_chain:
                    res_num += 1

        return entries

    def _write_raw_ligand_plddt_dump(
        self,
        record: Record,
        structure: StructureV2,
        outname: str,
        plddts: Tensor | None,
    ) -> None:
        if not self.boltz2 or plddts is None:
            return

        entries = self._raw_ligand_plddt_entries(structure, plddts)
        if not entries:
            return

        struct_dir = self.output_dir / record.id
        struct_dir.mkdir(exist_ok=True)

        json_path = struct_dir / f"raw_ligand_atom_plddts_{outname}.json"
        json_payload = {
            "record_id": record.id,
            "model_name": outname,
            "entry_count": len(entries),
            "entries": entries,
        }
        json_path.write_text(json.dumps(json_payload, indent=2))

        csv_path = struct_dir / f"raw_ligand_atom_plddts_{outname}.csv"
        header = [
            "chain",
            "res_name",
            "res_idx",
            "atom_name",
            "writer_token_index",
            "plddt_normalized",
            "plddt",
        ]
        lines = [",".join(header)]
        for entry in entries:
            lines.append(
                ",".join(
                    [
                        str(entry["chain"]),
                        str(entry["res_name"]),
                        str(entry["res_idx"]),
                        str(entry["atom_name"]),
                        str(entry["writer_token_index"]),
                        f"{float(entry['plddt_normalized']):.6f}",
                        f"{float(entry['plddt']):.6f}",
                    ]
                )
            )
        csv_path.write_text("\n".join(lines) + "\n")

    def write_on_batch_end(
        self,
        trainer,
        pl_module,
        prediction: dict[str, Tensor],
        batch_indices,
        batch: dict[str, Tensor],
        batch_idx: int,
        dataloader_idx: int = 0,
    ) -> None:
        super().write_on_batch_end(
            trainer,
            pl_module,
            prediction,
            batch_indices,
            batch,
            batch_idx,
            dataloader_idx,
        )
        if prediction["exception"]:
            return

        records: list[Record] = batch["record"]
        coords = prediction["coords"].unsqueeze(0)
        pad_masks = prediction["masks"]
        if "confidence_score" in prediction:
            argsort = torch.argsort(prediction["confidence_score"], descending=True)
            idx_to_rank = {idx.item(): rank for rank, idx in enumerate(argsort)}
        else:
            idx_to_rank = {i: i for i in range(len(records))}

        for record, coord, pad_mask in zip(records, coords, pad_masks):
            path = self.data_dir / f"{record.id}.npz"
            structure = StructureV2.load(path).remove_invalid_chains()
            for model_idx in range(coord.shape[0]):
                plddts = prediction["plddt"][model_idx] if "plddt" in prediction else None
                outname = f"{record.id}_model_{idx_to_rank[model_idx]}"
                self._write_raw_ligand_plddt_dump(record, structure, outname, plddts)


def _select_strategy(devices, num_records: int):
    strategy = "auto"
    if (isinstance(devices, int) and devices > 1) or (
        isinstance(devices, list) and len(devices) > 1
    ):
        start_method = (
            "fork"
            if os.name != "nt"
            else "spawn"
        )
        strategy = DDPStrategy(start_method=start_method)
        num_devices = len(devices) if isinstance(devices, list) else devices
        if num_records < num_devices:
            if isinstance(devices, list):
                devices = devices[: max(1, num_records)]
            else:
                devices = max(1, min(num_records, devices))
    return strategy, devices


def run_scoring(
    processed_dir: Path,
    output_dir: Path,
    cache_dir: Optional[Path] = None,
    checkpoint: Optional[Path] = None,
    devices: int = 1,
    accelerator: str = "gpu",
    num_workers: int = 2,
    output_format: str = "mmcif",
    recycling_steps: int = 20,
    sampling_steps: int = 1,
    diffusion_samples: int = 1,
    max_parallel_samples: int = 1,
    structure_refine: bool = False,
    write_full_pae: bool = False,
    step_scale: float = 1.5,
    no_kernels: bool = False,
    contact_guidance: bool = False,
    use_potentials: bool = False,
    reference_from_input: bool = False,
    sampling_init_from_input: bool = False,
    input_init_noise_scale: float = 0.0,
    sigma_max: float | None = None,
    noise_scale: float | None = None,
    gamma_0: float | None = None,
    gamma_min: float | None = None,
    seed: Optional[int] = None,
    trainer_precision: int | str | None = None,
) -> None:
    """Run confidence inference on a processed directory."""
    warnings.filterwarnings(
        "ignore", ".*that has Tensor Cores. To properly utilize them.*"
    )
    torch.set_grad_enabled(False)
    torch.set_float32_matmul_precision("highest")

    if seed is not None:
        seed_everything(seed)

    processed_dir = processed_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = Path(cache_dir or get_cache_path()).expanduser().resolve()

    # Set BOLTZ_CACHE environment variable to ensure consistent cache directory usage
    os.environ["BOLTZ_CACHE"] = str(cache_dir)

    mol_dir = cache_dir / "mols"
    if not mol_dir.exists():
        raise FileNotFoundError(
            f"Molecule directory not found: {mol_dir}. Please download Boltz2 assets."
        )

    checkpoint = checkpoint or (cache_dir / "boltz2_conf.ckpt")
    checkpoint = Path(checkpoint).expanduser().resolve()
    if not checkpoint.exists():
        raise FileNotFoundError(
            f"Checkpoint not found: {checkpoint}. Please download boltz2_conf.ckpt."
        )

    manifest_path = processed_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    manifest = Manifest.load(manifest_path)
    if not manifest.records:
        raise RuntimeError("No records found in manifest.")

    structure_dir = processed_dir / "structures"
    msa_dir = processed_dir / "msa"
    msa_dir.mkdir(parents=True, exist_ok=True)
    constraints_dir = processed_dir / "constraints"
    if not constraints_dir.exists():
        constraints_dir = None
    template_dir = processed_dir / "templates"
    if not template_dir.exists():
        template_dir = None
    extra_mols_dir = processed_dir / "mols"
    if not extra_mols_dir.exists():
        extra_mols_dir = None

    strategy, devices = _select_strategy(devices, len(manifest.records))

    diffusion_params = Boltz2DiffusionParams()
    diffusion_params.step_scale = step_scale
    if sigma_max is not None:
        diffusion_params.sigma_max = float(sigma_max)
    if noise_scale is not None:
        diffusion_params.noise_scale = float(noise_scale)
    if gamma_0 is not None:
        diffusion_params.gamma_0 = float(gamma_0)
    if gamma_min is not None:
        diffusion_params.gamma_min = float(gamma_min)
    pairformer_args = PairformerArgsV2()
    msa_args = MSAModuleArgs(
        subsample_msa=False,
        num_subsampled_msa=1024,
        use_paired_feature=True,
    )

    steering_args = BoltzSteeringParams()
    # Contact-guided ligand refinement needs Boltz2's geometry safeguards
    # (PoseBusters bounds, connectivity, chirality, planarity) to keep
    # small-molecule internal structure from drifting.
    ligand_geometry_guidance = bool(contact_guidance or use_potentials)
    steering_args.fk_steering = bool(use_potentials)
    steering_args.physical_guidance_update = ligand_geometry_guidance
    steering_args.contact_guidance_update = bool(contact_guidance or use_potentials)

    coordinate_source: Literal["sample", "input"] = "sample" if structure_refine else "input"
    predict_args = {
        "recycling_steps": recycling_steps,
        "sampling_steps": sampling_steps,
        "diffusion_samples": diffusion_samples,
        "max_parallel_samples": max_parallel_samples,
        "coordinate_source": coordinate_source,
        "reference_source": "input" if reference_from_input else "default",
        "sampling_init_source": "input" if sampling_init_from_input else "noise",
        "input_init_noise_scale": float(input_init_noise_scale),
        "write_confidence_summary": True,
        "write_full_pae": write_full_pae,
        "write_full_pde": False,
    }
    print(
        "[Info] Boltz2Score inference mode="
        f"{'structure_refine' if structure_refine else 'input_structure_only'}; "
        f"recycling_steps={recycling_steps}, sampling_steps={sampling_steps}, "
        f"diffusion_samples={diffusion_samples}, max_parallel_samples={max_parallel_samples}, "
        f"contact_guidance={'on' if contact_guidance else 'off'}, "
        f"ligand_geometry_guidance={'on' if ligand_geometry_guidance else 'off'}, "
        f"use_potentials={'on' if use_potentials else 'off'}, "
        f"reference_from_input={'on' if reference_from_input else 'off'}, "
        f"sampling_init={'input' if sampling_init_from_input else 'noise'}, "
        f"input_init_noise_scale={float(input_init_noise_scale):.4f}, "
        f"sigma_max={diffusion_params.sigma_max:.4f}, "
        f"noise_scale={diffusion_params.noise_scale:.4f}, "
        f"gamma_0={diffusion_params.gamma_0:.4f}, "
        f"gamma_min={diffusion_params.gamma_min:.4f}."
    )

    model_module = Boltz2ScoreModel.load_from_checkpoint(
        checkpoint,
        strict=True,
        predict_args=predict_args,
        map_location="cpu",
        diffusion_process_args=asdict(diffusion_params),
        ema=False,
        use_kernels=not no_kernels,
        pairformer_args=asdict(pairformer_args),
        msa_args=asdict(msa_args),
        steering_args=asdict(steering_args),
        confidence_prediction=True,
        affinity_prediction=False,
        skip_run_structure=not structure_refine,
        run_trunk_and_structure=True,
    )
    model_module.eval()

    data_module = Boltz2InferenceDataModule(
        manifest=manifest,
        target_dir=structure_dir,
        msa_dir=msa_dir,
        mol_dir=mol_dir,
        num_workers=num_workers,
        constraints_dir=constraints_dir,
        template_dir=template_dir,
        extra_mols_dir=extra_mols_dir,
        override_method=None,
    )

    pred_writer = Boltz2ScoreWriter(
        data_dir=structure_dir,
        output_dir=output_dir,
        output_format=output_format,
        boltz2=True,
        write_embeddings=False,
    )

    # Boltz-2 can hit occasional SVD convergence failures under bf16 AMP for
    # ill-conditioned inputs, so prefer fp32 unless explicitly overridden.
    resolved_precision: int | str
    if trainer_precision is not None:
        resolved_precision = 32 if str(trainer_precision).strip() == "32" else trainer_precision
    else:
        resolved_precision = 32

    trainer = Trainer(
        default_root_dir=output_dir,
        strategy=strategy,
        callbacks=[pred_writer],
        accelerator=accelerator,
        devices=devices,
        precision=resolved_precision,
        logger=False,
        enable_checkpointing=False,
        inference_mode=True,
    )

    trainer.predict(model_module, datamodule=data_module, return_predictions=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Boltz2 confidence inference with optional structure refinement."
    )
    parser.add_argument(
        "--processed_dir",
        required=True,
        type=str,
        help="Processed directory from the internal prepare-inputs stage.",
    )
    parser.add_argument(
        "--output_dir",
        required=True,
        type=str,
        help="Output directory for score results",
    )
    parser.add_argument(
        "--cache",
        type=str,
        default=None,
        help="Boltz cache directory (default: BOLTZ_CACHE or ~/.boltz)",
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        default=None,
        help="Path to boltz2_conf.ckpt (default: <cache>/boltz2_conf.ckpt)",
    )
    parser.add_argument("--devices", type=int, default=1)
    parser.add_argument(
        "--accelerator",
        type=str,
        default=None,
        help="Execution backend (default: auto-detect gpu/cpu at runtime).",
    )
    parser.add_argument("--num_workers", type=int, default=2)
    parser.add_argument(
        "--output_format",
        type=str,
        default="mmcif",
        choices=["pdb", "mmcif"],
    )
    parser.add_argument(
        "--recycling_steps",
        type=int,
        default=None,
        help="Override recycling steps. Defaults depend on refinement mode.",
    )
    parser.add_argument(
        "--sampling_steps",
        type=int,
        default=None,
        help="Override sampling steps. Defaults depend on refinement mode.",
    )
    parser.add_argument(
        "--diffusion_samples",
        type=int,
        default=None,
        help="Override diffusion sample count. Defaults depend on refinement mode.",
    )
    parser.add_argument("--max_parallel_samples", type=int, default=1)
    parser.add_argument(
        "--structure_refine",
        action="store_true",
        help="Enable diffusion structure refinement before confidence scoring.",
    )
    parser.add_argument(
        "--write_full_pae",
        action="store_true",
        help="Write full PAE matrices alongside confidence JSON outputs.",
    )
    parser.add_argument(
        "--no_structure_refine",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--step_scale", type=float, default=1.5)
    parser.add_argument("--no_kernels", action="store_true")
    parser.add_argument(
        "--contact_guidance",
        action="store_true",
        help="Enable Boltz2 contact-guidance potentials when inference constraints are present.",
    )
    parser.add_argument(
        "--use_potentials",
        action="store_true",
        help="Enable official Boltz2 potentials path (fk_steering + physical_guidance_update).",
    )
    parser.add_argument(
        "--reference_from_input",
        action="store_true",
        help="Use the input structure coordinates as ref_pos during prediction instead of canonical conformer coordinates.",
    )
    parser.add_argument(
        "--sampling_init_from_input",
        action="store_true",
        help="Initialize Boltz2 diffusion sampling from input coordinates instead of pure noise.",
    )
    parser.add_argument(
        "--input_init_noise_scale",
        type=float,
        default=0.0,
        help="Relative noise scale applied on top of input coordinates when --sampling_init_from_input is enabled.",
    )
    parser.add_argument(
        "--sigma_max",
        type=float,
        default=None,
        help="Override Boltz2 diffusion sigma_max during structure refinement.",
    )
    parser.add_argument(
        "--noise_scale",
        type=float,
        default=None,
        help="Override Boltz2 diffusion noise_scale during structure refinement.",
    )
    parser.add_argument(
        "--gamma_0",
        type=float,
        default=None,
        help="Override Boltz2 diffusion gamma_0 during structure refinement.",
    )
    parser.add_argument(
        "--gamma_min",
        type=float,
        default=None,
        help="Override Boltz2 diffusion gamma_min during structure refinement.",
    )
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--trainer_precision",
        type=str,
        default="32",
        help="Lightning trainer precision (default: 32). Use bf16-mixed/16-mixed only if stable.",
    )

    args = parser.parse_args()

    if args.structure_refine and args.no_structure_refine:
        raise ValueError("Cannot set both --structure_refine and --no_structure_refine.")
    if args.input_init_noise_scale < 0:
        raise ValueError("--input_init_noise_scale must be non-negative.")
    if args.accelerator is None:
        args.accelerator = "gpu" if torch.cuda.is_available() else "cpu"

    structure_refine = bool(args.structure_refine and not args.no_structure_refine)

    if structure_refine:
        resolved_recycling_steps = args.recycling_steps if args.recycling_steps is not None else 3
        resolved_sampling_steps = args.sampling_steps if args.sampling_steps is not None else 200
        resolved_diffusion_samples = args.diffusion_samples if args.diffusion_samples is not None else 5
    else:
        resolved_recycling_steps = args.recycling_steps if args.recycling_steps is not None else 7
        resolved_sampling_steps = args.sampling_steps if args.sampling_steps is not None else 1
        resolved_diffusion_samples = args.diffusion_samples if args.diffusion_samples is not None else 1

    run_scoring(
        processed_dir=Path(args.processed_dir),
        output_dir=Path(args.output_dir),
        cache_dir=Path(args.cache) if args.cache else None,
        checkpoint=Path(args.checkpoint) if args.checkpoint else None,
        devices=args.devices,
        accelerator=args.accelerator,
        num_workers=args.num_workers,
        output_format=args.output_format,
        recycling_steps=resolved_recycling_steps,
        sampling_steps=resolved_sampling_steps,
        diffusion_samples=resolved_diffusion_samples,
        max_parallel_samples=args.max_parallel_samples,
        structure_refine=structure_refine,
        write_full_pae=args.write_full_pae,
        step_scale=args.step_scale,
        no_kernels=args.no_kernels,
        contact_guidance=args.contact_guidance,
        use_potentials=args.use_potentials,
        reference_from_input=args.reference_from_input,
        sampling_init_from_input=args.sampling_init_from_input,
        input_init_noise_scale=args.input_init_noise_scale,
        sigma_max=args.sigma_max,
        noise_scale=args.noise_scale,
        gamma_0=args.gamma_0,
        gamma_min=args.gamma_min,
        seed=args.seed,
        trainer_precision=args.trainer_precision,
    )


if __name__ == "__main__":
    main()
