from __future__ import annotations

import argparse
from pathlib import Path

from core.flexible_optimization import run_flexible_optimization
from core.modes import DOCK_MODE
from utils.docking import (
    parse_smiles_input,
    prepare_dock_ligands,
    resolve_pocket_center,
    resolve_pocket_radius,
)


def run_high_level_mode_pipeline(args: argparse.Namespace, output_dir: Path) -> None:
    """Dispatch pose/refine/interface/dock to the flexible-optimization trial.

    Dock prepares its ligand SDF first (conformers from SMILES placed at the
    pocket); the other modes take the user's posed SDF as-is.
    """
    if args.mode == DOCK_MODE:
        _prepare_dock_input(args, output_dir)
    else:
        if args.input is not None:
            raise ValueError(
                f"--mode {args.mode!r} requires --protein_file + --ligand_file separate-input mode."
            )
        ligand_path = Path(args.ligand_file).expanduser().resolve()
        if ligand_path.suffix.lower() not in {".sdf", ".sd"}:
            raise ValueError(
                f"--mode {args.mode!r} requires an SDF ligand file. Got: {ligand_path.name}"
            )

    print(f"[Info] Running high-level pipeline mode: {args.mode}")
    print(f"[Info] Flexible optimization mode={args.mode} -> {output_dir}")
    run_flexible_optimization(args=args, output_dir=output_dir)


def _prepare_dock_input(args: argparse.Namespace, output_dir: Path) -> None:
    """Generate ETKDG conformers from SMILES, place them at the pocket, and
    wire the resulting SDF into ``args`` so the downstream flexible-optimization
    pipeline picks it up transparently.
    """
    if args.protein_file is None:
        raise ValueError("dock mode requires --protein_file.")

    print(f"\n[Dock] Step 1/3: Resolving pocket center ...")
    pocket_center = resolve_pocket_center(args)
    pocket_radius = resolve_pocket_radius(args)
    print(f"[Dock] Pocket center: {pocket_center}")
    print(f"[Dock] Pocket search radius: {pocket_radius:.1f} Å")

    print("[Dock] Step 2/3: Generating 3-D conformers from SMILES ...")
    smiles_entries = parse_smiles_input(
        smiles=args.ligand_smiles,
        smiles_file=args.ligand_smiles_file,
    )
    if not smiles_entries:
        raise ValueError("No SMILES entries provided for dock mode.")

    work_dir = output_dir / "_dock_input"
    dock_sdf = prepare_dock_ligands(
        smiles_entries=smiles_entries,
        pocket_center=pocket_center,
        seed=args.dock_seed,
        work_dir=work_dir,
        n_poses=args.dock_poses,
    )
    if args.dock_poses > 1:
        print(f"[Dock] Pose ensemble: {args.dock_poses} initial orientations per SMILES "
              f"(best scored pose is kept per ligand after refinement)")

    args.ligand_file = str(dock_sdf)

    # Dock mode uses a wider anchor_contact_cutoff to capture the full
    # pocket around the placed conformer.  Override the default (5.0)
    # with the resolved pocket radius.
    if args.anchor_contact_cutoff == 5.0:
        args.anchor_contact_cutoff = pocket_radius
        print(f"[Dock] Using anchor_contact_cutoff={pocket_radius:.1f} Å")
        # max_distance = cutoff + 1.0 Å
        min_max_distance = pocket_radius + 1.0
        if args.anchor_max_distance is None or args.anchor_max_distance < min_max_distance:
            args.anchor_max_distance = min_max_distance
            print(f"[Dock] Using anchor_max_distance={min_max_distance:.1f} Å")

    print("[Dock] Step 3/3: Launching anchored refinement ...\n")
