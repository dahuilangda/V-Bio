#!/usr/bin/env python3
"""Run Boltz2 confidence inference with optional structure refinement."""

from __future__ import annotations

import argparse
import os
import warnings
from dataclasses import asdict
from pathlib import Path
from typing import Literal, Optional

import torch
from pytorch_lightning import Trainer, seed_everything
from pytorch_lightning.strategies import DDPStrategy

from boltz.data.module.inferencev2 import Boltz2InferenceDataModule
from boltz.data.types import Manifest
from boltz.data.write.writer import BoltzWriter
from boltz.main import (
    Boltz2DiffusionParams,
    BoltzSteeringParams,
    MSAModuleArgs,
    PairformerArgsV2,
    get_cache_path,
)
from boltz.model.models.boltz2 import Boltz2


class Boltz2ScoreModel(Boltz2):
    """Boltz2 model with an explicit coordinate output policy."""

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

    def _resolve_coords(self, batch: dict, out: dict) -> torch.Tensor:
        source = str(self.predict_args.get("coordinate_source", "sample")).strip().lower()
        if source == "sample":
            return self._coords_from_structure_samples(out)
        if source == "input":
            return self._coords_from_input_batch(batch)
        raise ValueError(
            f"Unsupported coordinate_source={source!r}. Expected 'sample' or 'input'."
        )

    def predict_step(self, batch, batch_idx: int, dataloader_idx: int = 0) -> dict:
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
    step_scale: float = 1.5,
    no_kernels: bool = False,
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
    extra_mols_dir = processed_dir / "mols"
    if not extra_mols_dir.exists():
        extra_mols_dir = None

    strategy, devices = _select_strategy(devices, len(manifest.records))

    diffusion_params = Boltz2DiffusionParams()
    diffusion_params.step_scale = step_scale
    pairformer_args = PairformerArgsV2()
    msa_args = MSAModuleArgs(
        subsample_msa=False,
        num_subsampled_msa=1024,
        use_paired_feature=True,
    )

    steering_args = BoltzSteeringParams()
    steering_args.fk_steering = False
    steering_args.physical_guidance_update = False
    steering_args.contact_guidance_update = False

    coordinate_source: Literal["sample", "input"] = "sample" if structure_refine else "input"
    predict_args = {
        "recycling_steps": recycling_steps,
        "sampling_steps": sampling_steps,
        "diffusion_samples": diffusion_samples,
        "max_parallel_samples": max_parallel_samples,
        "coordinate_source": coordinate_source,
        "write_confidence_summary": True,
        "write_full_pae": False,
        "write_full_pde": False,
    }
    print(
        "[Info] Boltz2Score inference mode="
        f"{'structure_refine' if structure_refine else 'input_structure_only'}; "
        f"recycling_steps={recycling_steps}, sampling_steps={sampling_steps}, "
        f"diffusion_samples={diffusion_samples}, max_parallel_samples={max_parallel_samples}."
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
        constraints_dir=None,
        template_dir=None,
        extra_mols_dir=extra_mols_dir,
        override_method=None,
    )

    pred_writer = BoltzWriter(
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
        help="Processed directory from prepare_boltz2score_inputs.py",
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
        default="gpu" if torch.cuda.is_available() else "cpu",
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
        "--no_structure_refine",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--step_scale", type=float, default=1.5)
    parser.add_argument("--no_kernels", action="store_true")
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
        step_scale=args.step_scale,
        no_kernels=args.no_kernels,
        seed=args.seed,
        trainer_precision=args.trainer_precision,
    )


if __name__ == "__main__":
    main()
