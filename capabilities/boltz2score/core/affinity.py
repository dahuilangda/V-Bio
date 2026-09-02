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
from boltz.data.types import AffinityInfo, Manifest, Record, StructureV2
from boltz.data.write.writer import BoltzAffinityWriter
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


# Boltz-2 affinity head output  ≈  log10(IC50 in µM).  To present on the
# more intuitive pIC50 scale (-log10(IC50 in M)), subtract from 6.
AFFINITY_PIC50_OFFSET = 6.0

# Coefficients for the Boltz-2 molecular-weight correction (identical to
# the model's built-in calibration, re-applied here because the head is
# loaded with affinity_mw_correction=False).
AFFINITY_MW_MODEL_COEF = 1.03525938
AFFINITY_MW_COEF = -0.59992683
AFFINITY_MW_BIAS = 2.83288489


def _log10_um_to_pic50(value: float) -> float:
    """Convert log10(IC50 in µM) to pIC50 = 6 - value."""
    return AFFINITY_PIC50_OFFSET - float(value)


def _apply_mw_correction(value: float, molecular_weight: float) -> float:
    """Apply the Boltz-2 MW calibration: 1.035·v − 0.600·MW^0.3 + 2.833."""
    return (
        AFFINITY_MW_MODEL_COEF * float(value)
        + AFFINITY_MW_COEF * (float(molecular_weight) ** 0.3)
        + AFFINITY_MW_BIAS
    )


def _augment_affinity_result(
    result: dict[str, object],
    ligand_alignment: dict[str, object] | None,
) -> dict[str, object]:
    if isinstance(ligand_alignment, dict):
        for key in (
            "ligand_smiles",
            "ligand_chain",
        ):
            if key in ligand_alignment:
                result[key] = deepcopy(ligand_alignment[key])

    # Convert the raw affinity value (log10 IC50 µM) to pIC50 and add
    # the MW-corrected variants.  Works for both single-head and ensemble
    # (value1/value2) outputs.  The *_mw fields are only emitted when the
    # ligand molecular weight is actually known — otherwise they would be
    # indistinguishable from the uncorrected values.
    ligand_mw = float(result.get("ligand_mw", 0.0) or 0.0)

    raw_value = result.get("affinity_pred_value")
    if raw_value is not None:
        raw_value = float(raw_value)
        result["affinity_pic50"] = round(_log10_um_to_pic50(raw_value), 4)
        if ligand_mw > 0:
            mw_corrected = _apply_mw_correction(raw_value, ligand_mw)
            result["affinity_pred_value_mw"] = round(mw_corrected, 4)
            result["affinity_pic50_mw"] = round(_log10_um_to_pic50(mw_corrected), 4)

    # Ensemble head variants
    for suffix in ("1", "2"):
        raw_v = result.get(f"affinity_pred_value{suffix}")
        if raw_v is not None:
            raw_v = float(raw_v)
            result[f"affinity_pic50{suffix}"] = round(_log10_um_to_pic50(raw_v), 4)

    return result


def load_affinity_model(
    cache_dir: Path,
    *,
    affinity_refine: bool = False,
    checkpoint: Path | None = None,
    no_kernels: bool = False,
    recycling_steps: int = 1,
) -> Boltz2:
    """Load the Boltz2 affinity model from checkpoint.

    ``boltz2_aff.ckpt`` is ~2.2 GB; deserialising takes ~25-30 s.
    For batch scoring call this *once* and pass the model to
    :func:`run_affinity_prediction` via *model_module* so the checkpoint is
    read only once for the entire batch.

    *recycling_steps* controls the number of Pairformer recycling iterations.
    The upstream Boltz2 default is 5, but our ablation study shows that
    ``recycling_steps=1`` gives nearly identical affinity predictions
    (<0.5% change) at ~1.5-2× lower trunk compute cost.
    """
    cache_dir = cache_dir.expanduser().resolve()
    affinity_ckpt = (checkpoint or (cache_dir / "boltz2_aff.ckpt")).expanduser().resolve()
    if not affinity_ckpt.exists():
        raise FileNotFoundError(f"Affinity checkpoint not found: {affinity_ckpt}")

    # NOTE: sampling_steps must be >= 2 — the upstream sigma schedule divides
    # by (num_sampling_steps - 1), so 1 step yields 0/0 = NaN sigma which
    # poisons the whole affinity prediction (verified 2026-08-15).
    predict_affinity_args = {
        "recycling_steps": recycling_steps,
        "sampling_steps": 200 if affinity_refine else 2,
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

    from core.inference import _fast_load_from_checkpoint
    from core.model_cache import load_or_build_model

    explicit_kwargs = dict(
        predict_args=predict_affinity_args,
        diffusion_process_args=asdict(diffusion_params),
        ema=False,
        pairformer_args=asdict(pairformer_args),
        msa_args=asdict(msa_args),
        steering_args=asdict(steering_args),
        affinity_mw_correction=False,
        use_kernels=not no_kernels,
    )
    model_module = load_or_build_model(
        lambda: _fast_load_from_checkpoint(Boltz2, affinity_ckpt, explicit_kwargs=explicit_kwargs),
        cache_dir=cache_dir,
        checkpoint=affinity_ckpt,
        config={"model_class": "Boltz2_affinity", "explicit_kwargs": explicit_kwargs},
        prefix="affinity",
        log_tag="Boltz2 affinity model",
    )
    # Affinity refinement drives the stock Boltz2 sample(), whose sigma
    # schedule NaNs out for single-step sampling (see _safe_sample_schedule).
    from core.inference import _install_sample_schedule_patch  # local import avoids a cycle

    structure_module = getattr(model_module, "structure_module", None)
    if structure_module is not None and hasattr(structure_module, "sample"):
        _install_sample_schedule_patch(structure_module)
    return model_module


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
    model_module: Boltz2 | None = None,
    recycling_steps: int = 1,
    ligand_mw: float | None = None,
) -> dict | None:
    """Run the official Boltz2 affinity head.

    When *model_module* is supplied (loaded once via :func:`load_affinity_model`),
    the ~2.2 GB checkpoint is not re-read — the key optimisation for batch runs.
    """
    if seed is not None:
        seed_everything(seed)

    cache_dir = cache_dir.expanduser().resolve()
    affinity_ckpt = (checkpoint or (cache_dir / "boltz2_aff.ckpt")).expanduser().resolve()
    if model_module is None and not affinity_ckpt.exists():
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

    if model_module is None:
        model_module = load_affinity_model(
            cache_dir,
            affinity_refine=affinity_refine,
            checkpoint=affinity_ckpt,
            no_kernels=no_kernels,
            recycling_steps=recycling_steps,
        )
        model_module_was_passed = False
    else:
        # Update predict_args on the pre-loaded model so the recycling_steps
        # passed at call time takes effect (the model was loaded with the
        # default of 5; override it here for consistency).
        model_module.predict_args["recycling_steps"] = recycling_steps
        model_module_was_passed = True

    pred_writer = BoltzAffinityWriter(
        data_dir=str(processed_dir / "structures"),
        output_dir=str(output_dir),
    )

    # Precision selection mirrors the confidence model:
    # - "32"         → pure fp32 (safest, slowest)
    # - "bf16-mixed" → bf16 AMP (~25-30% faster, numerically safe for the
    #                   affinity head in fast mode since the SVD in
    #                   weighted_rigid_align is only invoked during
    #                   diffusion refinement, which is skipped in fast mode)
    # - None         → auto: bf16-mixed for fast affinity, fp32 for refine
    resolved_precision: int | str
    if trainer_precision is not None:
        resolved_precision = 32 if str(trainer_precision).strip() == "32" else trainer_precision
    elif affinity_refine:
        resolved_precision = 32  # fp32 for refinement (SVD risk)
    else:
        resolved_precision = "bf16-mixed"  # fast affinity: safe + fast

    from core.inference import _GPUCleanupCallback  # local import avoids a cycle

    trainer = Trainer(
        default_root_dir=output_dir / "affinity",
        callbacks=[pred_writer, _GPUCleanupCallback()],
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
    trainer.predict(model_module, datamodule=data_module, return_predictions=False)

    # Release GPU memory.  When model_module was passed from the caller
    # (batch-loaded), we only flush the cache — the caller owns the model.
    # When we loaded it ourselves (per-job), we delete it to free ~2.2 GB.
    import gc

    del trainer
    if model_module_was_passed:
        # Move the preloaded model back to CPU so the next scoring forward
        # pass has full GPU memory available.
        model_module.to("cpu")
    else:
        del model_module
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    affinity_result_path = _load_affinity_result_json(output_dir, record_id)
    result = json.loads(affinity_result_path.read_text())
    if ligand_mw is not None and "ligand_mw" not in result:
        result["ligand_mw"] = float(ligand_mw)
    result = _augment_affinity_result(result, ligand_alignment=ligand_alignment)
    affinity_result_path.write_text(json.dumps(result, indent=2) + "\n")
    return result
