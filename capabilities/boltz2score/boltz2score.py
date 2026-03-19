#!/usr/bin/env python3
"""Single-step Boltz2Score: input a PDB/mmCIF, output scores."""

from __future__ import annotations

import shutil
from pathlib import Path

from core.cli import build_execution_plan, build_main_parser, normalize_main_args
from core.job import run_single_job
from core.modes import SCORE_MODE
from core.pipeline import run_high_level_mode_pipeline
from utils.ligand_utils import load_ligand_entries_from_file, slugify_identifier


def main() -> None:
    parser = build_main_parser()
    args = normalize_main_args(parser.parse_args(), parser)

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.mode != SCORE_MODE:
        run_high_level_mode_pipeline(args, output_dir)
        return

    plan = build_execution_plan(
        args,
        parser,
        load_ligand_entries=load_ligand_entries_from_file,
        slugify_identifier=slugify_identifier,
    )
    try:
        for job in plan.jobs:
            print(f"[Info] Running Boltz2Score job: {job.record_id}")
            run_single_job(args=args, plan=plan, job=job)
    finally:
        if plan.cleanup_root:
            shutil.rmtree(plan.root_work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
