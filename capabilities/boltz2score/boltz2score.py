#!/usr/bin/env python3
"""Single-step Boltz2Score: input a PDB/mmCIF, output scores."""

from __future__ import annotations

import shutil
from pathlib import Path

from core.cli import build_main_parser, normalize_main_args, _validate_main_args
from core.modes import SCORE_MODE
from core.pipeline import run_high_level_mode_pipeline



def main() -> None:
    parser = build_main_parser()
    args = normalize_main_args(parser.parse_args(), parser)

    # Validate input contract for all modes (including dock) before dispatch.
    _validate_main_args(args, parser)

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.mode != SCORE_MODE:
        # High-level modes (pose/refine/interface/dock) only prepare inputs and
        # spawn a score-mode subprocess — heavy imports (~7s of torch + boltz)
        # are deferred into that subprocess.
        run_high_level_mode_pipeline(args, output_dir)
        return

    # Score-mode-only imports (torch/boltz stack, ~7s).
    from core.affinity import load_affinity_model
    from core.cli import build_execution_plan
    from core.inference import load_score_model
    from core.job import run_single_job
    from utils.ligand_utils import load_ligand_entries_from_file, slugify_identifier

    plan = build_execution_plan(
        args,
        parser,
        load_ligand_entries=load_ligand_entries_from_file,
        slugify_identifier=slugify_identifier,
    )

    # Load the ~2.2 GB Boltz2 confidence checkpoint ONCE and reuse it for
    # every ligand in the batch.  This avoids re-reading the checkpoint for
    # each job, which alone saves ~25-30 s per ligand.
    #
    # The steering configuration (contact_guidance, use_potentials) is
    # determined by mode-level flags, not per-job state — the per-job
    # variation is only in the input constraints (data-level), which are
    # handled by the data module, not the model.  So batch loading is safe
    # for all modes including structure_refine.
    checkpoint = Path(args.checkpoint) if args.checkpoint else None
    plan_contact_guidance = bool(plan.structure_refine and args.anchored_refine)
    model_module = load_score_model(
        cache_dir=plan.cache_dir,
        checkpoint=checkpoint,
        recycling_steps=plan.resolved_recycling_steps,
        sampling_steps=plan.resolved_sampling_steps,
        diffusion_samples=plan.resolved_diffusion_samples,
        max_parallel_samples=args.max_parallel_samples,
        structure_refine=plan.structure_refine,
        write_full_pae=args.compute_ipsae,
        step_scale=(args.step_scale if args.step_scale is not None else 1.5),
        no_kernels=args.no_kernels,
        contact_guidance=plan_contact_guidance,
        use_potentials=bool(plan.structure_refine and args.use_potentials),
        reference_from_input=bool(plan.structure_refine and args.reference_from_input),
        sampling_init_from_input=bool(plan.structure_refine and args.sampling_init_from_input),
        input_init_noise_scale=args.input_init_noise_scale,
        sigma_max=args.sigma_max,
        noise_scale=args.noise_scale,
        gamma_0=args.gamma_0,
        gamma_min=args.gamma_min,
    )

    # Similarly, pre-load the affinity model (~2.2 GB) once if affinity is
    # enabled.  This saves ~25-30 s per ligand of checkpoint deserialization.
    affinity_model = None
    if plan.run_affinity:
        # Default affinity recycling = 1 (ablation-optimized: matches upstream
        # R=5 quality at lower cost).  Override via --affinity_recycling_steps.
        affinity_recycling = int(args.affinity_recycling_steps or 1)
        try:
            # `checkpoint` is the CONFIDENCE checkpoint; the affinity head needs its own
            # boltz2_aff.ckpt. Passing the confidence path would strict-load mismatched head
            # weights and corrupt every affinity value in the batch — always let
            # load_affinity_model resolve its dedicated checkpoint.
            affinity_model = load_affinity_model(
                plan.cache_dir,
                affinity_refine=args.affinity_refine,
                checkpoint=None,
                no_kernels=args.no_kernels,
                recycling_steps=affinity_recycling,
            )
        except FileNotFoundError as exc:
            print(f"[Warning] {exc} — affinity will use per-job loading.")

    try:
        for job in plan.jobs:
            print(f"[Info] Running Boltz2Score job: {job.record_id}")
            run_single_job(
                args=args, plan=plan, job=job,
                model_module=model_module,
                affinity_model=affinity_model,
            )
    finally:
        if plan.cleanup_root:
            shutil.rmtree(plan.root_work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
