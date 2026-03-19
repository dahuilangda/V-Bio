from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from rdkit import Chem

from core.affinity import inspect_affinity_eligibility, prepare_affinity_record, run_affinity_prediction
from core.cli import ExecutionPlan, JobSpec
from core.inference import run_scoring
from core.results import compute_and_write_ipsae, rerank_diffusion_samples, write_chain_map
from core.prepare_inputs import prepare_inputs
from utils.ligand_utils import build_combined_input_from_parts, slugify_identifier
from utils.score_diagnostics import write_atom_coverage_diagnostics
from utils.structure_refinement import (
    configure_anchored_refine_constraints,
    configure_distal_self_templates,
    filter_structure_by_chains,
)
from utils.writer_compat import (
    normalize_duplicate_atom_ids_for_writer,
    validate_unique_atom_ids_for_writer,
)


def run_single_job(
    args: argparse.Namespace,
    plan: ExecutionPlan,
    job: JobSpec,
) -> None:
    record_id = job.record_id
    input_path = job.input_path
    protein_path = job.protein_path
    ligand_entry = job.ligand_entry
    job_prefix = slugify_identifier(record_id, "job")
    work_dir = Path(tempfile.mkdtemp(prefix=f"{job_prefix}_", dir=plan.root_work_dir))

    preloaded_custom_mols: dict[str, Chem.Mol] | None = None
    reference_ligand_mol_for_alignment: Chem.Mol | None = None
    resolved_ligand_smiles_map = dict(plan.ligand_smiles_map or {})

    if protein_path is not None and ligand_entry is not None:
        ligand_mol = Chem.Mol(ligand_entry["mol"])
        ligand_label = str(ligand_entry.get("label") or Path(args.ligand_file).name)
        input_path, preloaded_custom_mols, reference_ligand_mol_for_alignment, resolved_ligand_smiles_map = (
            build_combined_input_from_parts(
                protein_path=protein_path,
                ligand_mol=ligand_mol,
                ligand_source_label=ligand_label,
                ligand_smiles_map=resolved_ligand_smiles_map,
                work_dir=work_dir,
                record_id=record_id,
            )
        )

    if input_path is None:
        raise RuntimeError("No input structure resolved for scoring job.")
    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")

    resolved_ligand_chain_id = plan.ligand_chains[0] if plan.ligand_chains else None
    if resolved_ligand_chain_id is None and reference_ligand_mol_for_alignment is not None:
        # Separate protein+ligand inputs are merged by staging the explicit ligand as chain L.
        resolved_ligand_chain_id = "L"

    if plan.run_affinity:
        shared_subset_input = work_dir / f"{record_id}_shared_subset.cif"
        filter_structure_by_chains(
            input_path=input_path,
            target_chains=plan.target_chains,
            ligand_chains=plan.ligand_chains,
            output_path=shared_subset_input,
        )
        input_path = shared_subset_input
        print(
            f"[Info] Locked shared chain subset for Boltz2Score + Boltzina: "
            f"target={list(plan.target_chains)}, ligand={list(plan.ligand_chains)}, file={input_path}"
        )

    input_dir = work_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    staged_suffix = input_path.suffix if input_path.suffix else ".cif"
    staged_input = input_dir / f"{record_id}{staged_suffix.lower()}"
    if staged_input.exists():
        staged_input.unlink()
    shutil.copy2(input_path, staged_input)

    renamed = normalize_duplicate_atom_ids_for_writer(staged_input)
    if renamed:
        print(
            f"[Info] Normalized {renamed} duplicate atom IDs in "
            f"{staged_input.name} for mmCIF writer compatibility."
        )
    validate_unique_atom_ids_for_writer(staged_input)

    prepare_inputs(
        input_dir=input_dir,
        out_dir=work_dir,
        cache_dir=plan.cache_dir,
        recursive=False,
        preloaded_custom_mols=preloaded_custom_mols,
        ligand_smiles_map=resolved_ligand_smiles_map if resolved_ligand_smiles_map else None,
        use_msa_server=args.use_msa_server,
        msa_server_url=args.msa_server_url,
        msa_pairing_strategy=args.msa_pairing_strategy,
        max_msa_seqs=args.max_msa_seqs,
        self_template=bool(plan.structure_refine and args.self_template),
        self_template_threshold=args.self_template_threshold,
    )

    run_affinity = bool(plan.run_affinity)

    if run_affinity:
        affinity_eligibility = inspect_affinity_eligibility(
            processed_dir=work_dir / "processed",
            record_id=record_id,
            requested_ligand_chain_id=resolved_ligand_chain_id,
        )
        if not bool(affinity_eligibility.get("eligible")):
            run_affinity = False
            print(
                "[Warning] Skipping affinity prediction: "
                f"{affinity_eligibility.get('reason')}"
            )

    if run_affinity:
        affinity_summary = prepare_affinity_record(
            processed_dir=work_dir / "processed",
            cache_dir=plan.cache_dir,
            record_id=record_id,
            requested_ligand_chain_id=resolved_ligand_chain_id,
            reference_ligand_mol=reference_ligand_mol_for_alignment,
        )
        print(
            "[Info] Prepared Boltz2 affinity metadata: "
            f"ligand_chain={affinity_summary['ligand_chain_name']} "
            f"(chain_id={affinity_summary['ligand_chain_id']}), "
            f"ligand_mw={affinity_summary['ligand_mw']:.3f}."
        )

    anchored_guidance_enabled = False
    reference_from_input_enabled = bool(plan.structure_refine and args.reference_from_input)
    sampling_init_from_input_enabled = bool(plan.structure_refine and args.sampling_init_from_input)
    if plan.structure_refine and args.anchored_refine:
        anchored_summary = configure_anchored_refine_constraints(
            processed_dir=work_dir / "processed",
            record_id=record_id,
            requested_ligand_chain_id=resolved_ligand_chain_id,
            requested_target_chains=plan.target_chains,
            contact_cutoff=args.anchor_contact_cutoff,
            max_distance=args.anchor_max_distance,
            max_residues=args.anchor_max_residues,
            pose_anchor_atoms=args.pose_anchor_atoms,
            pose_anchor_slack=args.pose_anchor_slack,
            anchor_strategy=args.anchor_strategy,
            output_dir=plan.output_dir,
        )
        anchored_guidance_enabled = True
        print(
            "[Info] Anchored refinement constraints prepared: "
            f"ligand={anchored_summary['ligand_chain_name']} "
            f"(asym_id={anchored_summary['ligand_asym_id']}), "
            f"contacts={anchored_summary['contact_residue_count']}, "
            f"cutoff={args.anchor_contact_cutoff:.1f}A, "
            f"max_distance={args.anchor_max_distance:.1f}A."
        )
        if args.self_template and args.template_exclude_pocket_margin >= 0:
            template_summary = configure_distal_self_templates(
                processed_dir=work_dir / "processed",
                record_id=record_id,
                contact_rows=anchored_summary["contact_residues"],
                template_threshold=args.self_template_threshold,
                pocket_margin=args.template_exclude_pocket_margin,
            )
            print(
                "[Info] Distal self-template prepared: "
                f"spans={template_summary['template_span_count']}, "
                f"margin={args.template_exclude_pocket_margin}, "
                f"threshold={args.self_template_threshold:.1f}A."
            )

    run_scoring(
        processed_dir=work_dir / "processed",
        output_dir=plan.output_dir,
        cache_dir=plan.cache_dir,
        checkpoint=Path(args.checkpoint) if args.checkpoint else None,
        devices=args.devices,
        accelerator=args.accelerator,
        num_workers=args.num_workers,
        output_format=plan.resolved_output_format,
        recycling_steps=plan.resolved_recycling_steps,
        sampling_steps=plan.resolved_sampling_steps,
        diffusion_samples=plan.resolved_diffusion_samples,
        max_parallel_samples=args.max_parallel_samples,
        structure_refine=plan.structure_refine,
        write_full_pae=args.compute_ipsae,
        step_scale=args.step_scale,
        no_kernels=args.no_kernels,
        contact_guidance=anchored_guidance_enabled,
        use_potentials=bool(plan.structure_refine and args.use_potentials),
        reference_from_input=reference_from_input_enabled,
        sampling_init_from_input=sampling_init_from_input_enabled,
        input_init_noise_scale=args.input_init_noise_scale,
        sigma_max=args.sigma_max,
        noise_scale=args.noise_scale,
        gamma_0=args.gamma_0,
        gamma_min=args.gamma_min,
        seed=args.seed,
        trainer_precision=args.trainer_precision,
    )

    write_chain_map(
        processed_dir=work_dir / "processed",
        output_dir=plan.output_dir,
        record_id=record_id,
    )
    ligand_alignment = write_atom_coverage_diagnostics(
        processed_dir=work_dir / "processed",
        output_dir=plan.output_dir,
        record_id=record_id,
        requested_ligand_chain_id=resolved_ligand_chain_id,
        ligand_smiles_map=resolved_ligand_smiles_map if resolved_ligand_smiles_map else None,
        reference_ligand_mol=reference_ligand_mol_for_alignment,
    )

    if args.compute_ipsae:
        compute_and_write_ipsae(
            output_dir=plan.output_dir,
            record_id=record_id,
            pae_cutoff=args.ipsae_pae_cutoff,
            dist_cutoff=args.ipsae_dist_cutoff,
        )

    rerank_summary = rerank_diffusion_samples(
        output_dir=plan.output_dir,
        record_id=record_id,
    )
    if rerank_summary is not None and not rerank_summary["selected_is_writer_default"]:
        print(
            "[Info] Interface-aware reranking selected "
            f"{rerank_summary['selected_model']} over {rerank_summary['default_writer_model']}."
        )

    if run_affinity:
        run_affinity_prediction(
            processed_dir=work_dir / "processed",
            output_dir=plan.output_dir,
            cache_dir=plan.cache_dir,
            record_id=record_id,
            accelerator=args.accelerator,
            devices=args.devices,
            affinity_refine=args.affinity_refine,
            checkpoint=None,
            seed=args.seed,
            num_workers=args.num_workers,
            trainer_precision=args.trainer_precision,
            ligand_alignment=ligand_alignment,
            no_kernels=args.no_kernels,
        )
