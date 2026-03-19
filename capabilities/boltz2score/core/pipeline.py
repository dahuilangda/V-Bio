from __future__ import annotations

import argparse
from pathlib import Path

from core.flexible_optimization import run_flexible_optimization
from core.modes import SCORE_MODE


def run_high_level_mode_pipeline(args: argparse.Namespace, output_dir: Path) -> None:
    if args.input is not None:
        raise ValueError(
            f"--mode {args.mode!r} currently requires --protein_file + --ligand_file separate-input mode."
        )
    ligand_path = Path(args.ligand_file).expanduser().resolve()
    if ligand_path.suffix.lower() not in {".sdf", ".sd"}:
        raise ValueError(
            f"--mode {args.mode!r} currently requires an SDF ligand file. Got: {ligand_path.name}"
        )

    print(f"[Info] Running high-level pipeline mode: {args.mode}")
    print(f"[Info] Step 1/1: flexible optimization mode={args.mode} -> {output_dir}")
    run_flexible_optimization(
        args=args,
        output_dir=output_dir,
    )
