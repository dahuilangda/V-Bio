from __future__ import annotations

import json
import pickle
from dataclasses import asdict, replace
from copy import deepcopy
from pathlib import Path

import torch
from pytorch_lightning import Trainer, seed_everything
from rdkit import Chem
from rdkit.Chem import Descriptors

from boltz.data import const
from boltz.data.module.inferencev2 import Boltz2InferenceDataModule
from boltz.data.types import AffinityInfo, Manifest, Record
from boltz.data.write.writer import BoltzAffinityWriter
import boltz.model.loss.diffusionv2 as diffusionv2_loss_mod
import boltz.model.modules.diffusionv2 as diffusionv2_mod
from boltz.main import Boltz2DiffusionParams, BoltzSteeringParams, MSAModuleArgs, PairformerArgsV2
from boltz.model.models.boltz2 import Boltz2


def _chain_name_matches(candidate: str, requested: str) -> bool:
    cand = str(candidate or "").strip().upper()
    req = str(requested or "").strip().upper()
    if not cand or not req:
        return False
    return cand == req or cand.startswith(f"{req}X") or req.startswith(f"{cand}X")


def _load_manifest_record(processed_dir: Path, record_id: str) -> tuple[Manifest, Record]:
    manifest_path = processed_dir / "manifest.json"
    manifest = Manifest.load(manifest_path)
    for record in manifest.records:
        if record.id == record_id:
            return manifest, record
    raise KeyError(f"Record {record_id!r} not found in {manifest_path}")


def inspect_affinity_eligibility(
    *,
    processed_dir: Path,
    record_id: str,
    requested_ligand_chain_id: str | None,
) -> dict[str, object]:
    _, record = _load_manifest_record(processed_dir, record_id)
    ligand_chains = [
        chain
        for chain in record.chains
        if int(chain.mol_type) == const.chain_type_ids["NONPOLYMER"] and bool(chain.valid)
    ]
    available = [str(chain.chain_name).strip() for chain in ligand_chains]
    if not ligand_chains:
        return {
            "eligible": False,
            "reason": (
                "No nonpolymer ligand chain found after Boltz preprocessing. "
                "Affinity prediction is currently enabled only for protein-small-molecule complexes, "
                "not protein-peptide or protein-protein inputs."
            ),
            "available_ligand_chains": available,
        }
    if requested_ligand_chain_id:
        requested = str(requested_ligand_chain_id).strip()
        matches = [
            chain
            for chain in ligand_chains
            if _chain_name_matches(str(chain.chain_name).strip(), requested)
        ]
        if not matches:
            return {
                "eligible": False,
                "reason": (
                    f"Requested ligand chain {requested!r} did not resolve to a small-molecule chain. "
                    f"Available small-molecule chains: {available or 'none'}."
                ),
                "available_ligand_chains": available,
            }
    return {
        "eligible": True,
        "available_ligand_chains": available,
    }


def _select_affinity_ligand_chain(
    record: Record,
    requested_ligand_chain_id: str | None,
) -> object:
    ligand_chains = [
        chain
        for chain in record.chains
        if int(chain.mol_type) == const.chain_type_ids["NONPOLYMER"] and bool(chain.valid)
    ]
    if requested_ligand_chain_id:
        requested = str(requested_ligand_chain_id).strip()
        matches = [
            chain
            for chain in ligand_chains
            if _chain_name_matches(str(chain.chain_name).strip(), requested)
        ]
        if len(matches) == 1:
            return matches[0]
        available = [str(chain.chain_name) for chain in ligand_chains]
        raise ValueError(
            f"Requested ligand chain {requested!r} not found in record {record.id}. "
            f"Available ligand chains: {available or 'none'}."
        )
    if len(ligand_chains) != 1:
        available = [str(chain.chain_name) for chain in ligand_chains]
        raise ValueError(
            "Affinity prediction currently requires exactly one ligand chain when "
            "--ligand_chain is not provided. "
            f"Record={record.id}, available ligand chains={available or 'none'}."
        )
    return ligand_chains[0]


def _residue_names_for_chain(processed_dir: Path, record_id: str, chain_id: int) -> list[str]:
    structure = StructureV2.load(processed_dir / "structures" / f"{record_id}.npz")
    names: list[str] = []
    for chain in structure.chains:
        if int(chain["asym_id"]) != int(chain_id):
            continue
        res_start = int(chain["res_idx"])
        res_end = res_start + int(chain["res_num"])
        for residue in structure.residues[res_start:res_end]:
            name = str(residue["name"] or "").strip()
            if name:
                names.append(name)
        break
    return names


def _load_mol_from_processed_cache(processed_dir: Path, record_id: str, residue_names: list[str]) -> Chem.Mol | None:
    mols_path = processed_dir / "mols" / f"{record_id}.pkl"
    if not mols_path.exists():
        return None
    with mols_path.open("rb") as handle:
        payload = pickle.load(handle)  # noqa: S301
    if not isinstance(payload, dict):
        return None
    normalized_residue_names = {str(name or "").strip() for name in residue_names if str(name or "").strip()}
    for key, value in payload.items():
        key_name = str(key or "").strip()
        if normalized_residue_names and key_name not in normalized_residue_names:
            continue
        if isinstance(value, Chem.Mol):
            return Chem.Mol(value)
    for value in payload.values():
        if isinstance(value, Chem.Mol):
            return Chem.Mol(value)
    return None


def _load_mol_from_boltz_cache(cache_dir: Path, residue_names: list[str]) -> Chem.Mol | None:
    normalized_residue_names = [str(name or "").strip() for name in residue_names if str(name or "").strip()]
    for residue_name in normalized_residue_names:
        mol_path = cache_dir / "mols" / f"{residue_name}.pkl"
        if not mol_path.exists():
            continue
        with mol_path.open("rb") as handle:
            payload = pickle.load(handle)  # noqa: S301
        if isinstance(payload, Chem.Mol):
            return Chem.Mol(payload)
        if isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, Chem.Mol):
                    return Chem.Mol(value)
    return None


def _resolve_affinity_ligand_mw(
    processed_dir: Path,
    record_id: str,
    ligand_chain_id: int,
    cache_dir: Path,
    reference_ligand_mol: Chem.Mol | None,
) -> float:
    mol: Chem.Mol | None = None
    if reference_ligand_mol is not None:
        mol = Chem.Mol(reference_ligand_mol)
    if mol is None:
        residue_names = _residue_names_for_chain(processed_dir, record_id, ligand_chain_id)
        mol = _load_mol_from_processed_cache(processed_dir, record_id, residue_names)
        if mol is None:
            mol = _load_mol_from_boltz_cache(cache_dir, residue_names)
    if mol is None:
        raise RuntimeError(
            "Failed to resolve ligand molecule for affinity MW calculation. "
            f"record_id={record_id}, ligand_chain_id={ligand_chain_id}"
        )
    mol_no_h = Chem.RemoveHs(Chem.Mol(mol))
    return float(Descriptors.MolWt(mol_no_h))


def prepare_affinity_record(
    *,
    processed_dir: Path,
    cache_dir: Path,
    record_id: str,
    requested_ligand_chain_id: str | None,
    reference_ligand_mol: Chem.Mol | None,
) -> dict[str, object]:
    manifest, record = _load_manifest_record(processed_dir, record_id)
    ligand_chain = _select_affinity_ligand_chain(record, requested_ligand_chain_id)
    ligand_mw = _resolve_affinity_ligand_mw(
        processed_dir=processed_dir,
        record_id=record_id,
        ligand_chain_id=int(ligand_chain.chain_id),
        cache_dir=cache_dir,
        reference_ligand_mol=reference_ligand_mol,
    )
    affinity_info = AffinityInfo(
        chain_id=int(ligand_chain.chain_id),
        mw=float(ligand_mw),
    )
    updated_records = [
        replace(existing_record, affinity=affinity_info)
        if existing_record.id == record_id
        else existing_record
        for existing_record in manifest.records
    ]
    updated_manifest = Manifest(records=updated_records)
    updated_manifest.dump(processed_dir / "manifest.json")
    return {
        "record_id": record_id,
        "ligand_chain_name": str(ligand_chain.chain_name),
        "ligand_chain_id": int(ligand_chain.chain_id),
        "ligand_mw": float(ligand_mw),
    }


def _load_affinity_result_json(output_dir: Path, record_id: str) -> Path:
    result_path = output_dir / record_id / f"affinity_{record_id}.json"
    if not result_path.exists():
        raise FileNotFoundError(f"Expected affinity result not found: {result_path}")
    return result_path


def _augment_affinity_result(
    result: dict[str, object],
    ligand_alignment: dict[str, object] | None,
) -> dict[str, object]:
    if not isinstance(ligand_alignment, dict):
        return result

    for key in (
        "ligand_smiles",
        "ligand_chain",
    ):
        if key in ligand_alignment:
            result[key] = deepcopy(ligand_alignment[key])

    return result


def _stable_weighted_rigid_align(
    true_coords: torch.Tensor,
    pred_coords: torch.Tensor,
    weights: torch.Tensor,
    mask: torch.Tensor | None = None,
) -> torch.Tensor:
    if mask is None:
        mask = torch.ones_like(weights, dtype=torch.bool)
    mask_bool = mask.to(dtype=torch.bool)
    weight_values = (weights * mask_bool).to(dtype=torch.float64)
    true_values = true_coords.to(dtype=torch.float64)
    pred_values = pred_coords.to(dtype=torch.float64)

    batch_size = true_values.shape[0]
    aligned_batches: list[torch.Tensor] = []
    for batch_index in range(batch_size):
        batch_true_all = true_values[batch_index]
        batch_pred_all = pred_values[batch_index]
        finite_true = torch.isfinite(batch_true_all).all(dim=-1)
        finite_pred = torch.isfinite(batch_pred_all).all(dim=-1)
        valid = mask_bool[batch_index] & finite_true & finite_pred

        fallback_coords = torch.where(
            torch.isfinite(batch_pred_all),
            batch_pred_all,
            torch.where(
                torch.isfinite(batch_true_all),
                batch_true_all,
                torch.zeros_like(batch_true_all),
            ),
        )

        if int(valid.sum().item()) < 3:
            aligned_batches.append(fallback_coords)
            continue

        batch_weights = weight_values[batch_index, valid]
        weight_sum = batch_weights.sum()
        if not torch.isfinite(weight_sum) or float(weight_sum.item()) <= 0:
            aligned_batches.append(fallback_coords)
            continue

        batch_true = batch_true_all[valid]
        batch_pred = batch_pred_all[valid]

        true_centroid = (batch_true * batch_weights[:, None]).sum(dim=0, keepdim=True) / weight_sum
        pred_centroid = (batch_pred * batch_weights[:, None]).sum(dim=0, keepdim=True) / weight_sum
        true_centered = batch_true - true_centroid
        pred_centered = batch_pred - pred_centroid

        cov = (batch_weights[:, None] * pred_centered).transpose(0, 1) @ true_centered
        cov_cpu = cov.to(device="cpu", dtype=torch.float64)
        jitter = max(float(cov_cpu.abs().max().item()), 1.0) * 1e-8
        cov_cpu = cov_cpu + torch.eye(3, dtype=torch.float64) * jitter

        u, _, vh = torch.linalg.svd(cov_cpu, full_matrices=False)
        rotation = u @ vh
        if torch.det(rotation) < 0:
            u[:, -1] *= -1.0
            rotation = u @ vh
        rotation = rotation.to(device=true_values.device, dtype=torch.float64)

        full_true = torch.where(
            finite_true[:, None],
            batch_true_all,
            fallback_coords,
        )
        aligned = (full_true - true_centroid) @ rotation.transpose(0, 1) + pred_centroid
        invalid_points = ~(finite_true & finite_pred)
        if torch.any(invalid_points):
            aligned[invalid_points] = fallback_coords[invalid_points]
        aligned_batches.append(aligned)

    return torch.stack(aligned_batches, dim=0).to(dtype=true_coords.dtype, device=true_coords.device)


def _install_stable_align_patch() -> None:
    if getattr(diffusionv2_mod, "_boltz2score_stable_align_patch", False):
        return
    diffusionv2_mod.weighted_rigid_align = _stable_weighted_rigid_align
    diffusionv2_loss_mod.weighted_rigid_align = _stable_weighted_rigid_align
    diffusionv2_mod._boltz2score_stable_align_patch = True


def run_affinity_prediction(
    *,
    processed_dir: Path,
    output_dir: Path,
    cache_dir: Path,
    record_id: str,
    accelerator: str,
    devices: int,
    affinity_refine: bool = False,
    checkpoint: Path | None = None,
    seed: int | None = None,
    num_workers: int = 0,
    trainer_precision: int | str | None = None,
    ligand_alignment: dict[str, object] | None = None,
    no_kernels: bool = False,
) -> dict | None:
    if seed is not None:
        seed_everything(seed)

    cache_dir = cache_dir.expanduser().resolve()
    affinity_ckpt = (checkpoint or (cache_dir / "boltz2_aff.ckpt")).expanduser().resolve()
    if not affinity_ckpt.exists():
        print(f"[Warning] Affinity checkpoint not found: {affinity_ckpt}. Skipping affinity.")
        return None

    manifest, record = _load_manifest_record(processed_dir, record_id)
    if record.affinity is None:
        raise RuntimeError(
            f"Affinity requested for {record_id}, but manifest affinity metadata was not prepared."
        )

    pre_affinity_path = output_dir / record_id / f"pre_affinity_{record_id}.npz"
    if not pre_affinity_path.exists():
        raise FileNotFoundError(
            f"Missing pre-affinity structure snapshot required by Boltz2 affinity: {pre_affinity_path}"
        )

    manifest_filtered = Manifest(records=[record])

    template_dir = processed_dir / "templates"
    if not template_dir.exists():
        template_dir = None
    constraints_dir = processed_dir / "constraints"
    if not constraints_dir.exists():
        constraints_dir = None
    extra_mols_dir = processed_dir / "mols"
    if not extra_mols_dir.exists():
        extra_mols_dir = None

    data_module = Boltz2InferenceDataModule(
        manifest=manifest_filtered,
        target_dir=output_dir,
        msa_dir=processed_dir / "msa",
        mol_dir=cache_dir / "mols",
        num_workers=num_workers,
        constraints_dir=constraints_dir,
        template_dir=template_dir,
        extra_mols_dir=extra_mols_dir,
        override_method="other",
        affinity=True,
    )

    predict_affinity_args = {
        "recycling_steps": 5,
        "sampling_steps": 200 if affinity_refine else 1,
        "diffusion_samples": 3 if affinity_refine else 1,
        "max_parallel_samples": 1,
        "write_confidence_summary": False,
        "write_full_pae": False,
        "write_full_pde": False,
    }

    diffusion_params = Boltz2DiffusionParams()
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

    model_module = Boltz2.load_from_checkpoint(
        affinity_ckpt,
        strict=True,
        predict_args=predict_affinity_args,
        map_location="cpu",
        diffusion_process_args=asdict(diffusion_params),
        ema=False,
        pairformer_args=asdict(pairformer_args),
        msa_args=asdict(msa_args),
        steering_args=asdict(steering_args),
        affinity_mw_correction=False,
        use_kernels=not no_kernels,
    )
    model_module.eval()

    pred_writer = BoltzAffinityWriter(
        data_dir=str(processed_dir / "structures"),
        output_dir=str(output_dir),
    )

    resolved_precision: int | str
    if trainer_precision is not None:
        resolved_precision = 32 if str(trainer_precision).strip() == "32" else trainer_precision
    else:
        resolved_precision = 32

    trainer = Trainer(
        default_root_dir=output_dir / "affinity",
        callbacks=[pred_writer],
        accelerator=accelerator,
        devices=devices,
        precision=resolved_precision,
        logger=False,
        enable_checkpointing=False,
        inference_mode=True,
    )

    print(
        "[Info] Running official Boltz2 affinity head "
        f"on pre_affinity coordinates for {record_id}."
    )
    _install_stable_align_patch()
    trainer.predict(model_module, datamodule=data_module, return_predictions=False)

    affinity_result_path = _load_affinity_result_json(output_dir, record_id)
    result = json.loads(affinity_result_path.read_text())
    result = _augment_affinity_result(result, ligand_alignment=ligand_alignment)
    affinity_result_path.write_text(json.dumps(result, indent=2) + "\n")
    return result
