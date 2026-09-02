from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.modes import SCORE_MODE, DOCK_MODE, mode_help_text, normalize_mode_name


@dataclass(frozen=True)
class JobSpec:
    record_id: str
    input_path: Path | None
    protein_path: Path | None
    ligand_entry: dict[str, object] | None


@dataclass(frozen=True)
class ExecutionPlan:
    output_dir: Path
    cache_dir: Path
    root_work_dir: Path
    cleanup_root: bool
    jobs: tuple[JobSpec, ...]
    ligand_smiles_map: dict[str, str]
    target_chains: tuple[str, ...]
    ligand_chains: tuple[str, ...]
    run_affinity: bool
    structure_refine: bool
    resolved_recycling_steps: int
    resolved_sampling_steps: int
    resolved_diffusion_samples: int
    resolved_output_format: str


def _parse_chain_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_ligand_index_selection(raw_value: str | None) -> tuple[int, ...]:
    if not raw_value:
        return ()
    selected: list[int] = []
    seen: set[int] = set()
    for chunk in str(raw_value).split(","):
        token = chunk.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            try:
                start = int(start_text.strip())
                end = int(end_text.strip())
            except ValueError as exc:
                raise ValueError(
                    f"Invalid --ligand_indices range {token!r}. Use 1-based integers like 1,3-5."
                ) from exc
            if start <= 0 or end <= 0:
                raise ValueError("--ligand_indices values must be positive 1-based integers.")
            if end < start:
                raise ValueError(
                    f"Invalid --ligand_indices range {token!r}. Range end must be >= start."
                )
            values = range(start, end + 1)
        else:
            try:
                value = int(token)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid --ligand_indices value {token!r}. Use 1-based integers like 1,3-5."
                ) from exc
            if value <= 0:
                raise ValueError("--ligand_indices values must be positive 1-based integers.")
            values = (value,)
        for value in values:
            if value not in seen:
                selected.append(value)
                seen.add(value)
    return tuple(selected)


def _parse_ligand_smiles_map(raw_value: str | None) -> dict[str, str]:
    ligand_smiles_map: dict[str, str] = {}
    if not raw_value:
        return ligand_smiles_map
    try:
        payload = json.loads(raw_value)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid --ligand_smiles_map JSON: {exc}") from exc
    if not isinstance(payload, dict):
        return ligand_smiles_map
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        normalized_key = key.strip()
        normalized_value = value.strip()
        if normalized_key and normalized_value:
            ligand_smiles_map[normalized_key] = normalized_value
    return ligand_smiles_map


def _add_input_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("Input")
    group.add_argument("--input", type=str, help="Input structure file (.pdb/.cif/.mmcif)")
    group.add_argument("--output_dir", required=True, type=str, help="Output directory for score results")
    group.add_argument("--protein_file", type=str, default=None, help="Protein structure file (.pdb/.cif/.mmcif) for separate input mode")
    group.add_argument("--ligand_file", type=str, default=None, help="Ligand structure file (.sdf/.mol/.mol2/.pdb); multi-molecule SDF is supported")
    group.add_argument("--ligand_indices", type=str, default=None, help="Optional 1-based ligand entry selection for multi-molecule inputs, e.g. 1,3-5. Default: all ligands.")
    group.add_argument("--mode", type=str, default=SCORE_MODE, help=mode_help_text())


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("Runtime")
    group.add_argument("--cache", type=str, default=None, help="Boltz cache directory (default: BOLTZ_CACHE or ~/.boltz)")
    group.add_argument("--checkpoint", type=str, default=None, help="Path to boltz2_conf.ckpt (default: <cache>/boltz2_conf.ckpt)")
    group.add_argument("--devices", type=int, default=1)
    group.add_argument("--accelerator", type=str, default=None, help="Execution backend (default: auto-detect gpu/cpu at runtime).")
    group.add_argument("--num_workers", type=int, default=0, help="DataLoader workers (default: 0 for compatibility)")
    group.add_argument("--output_format", type=str, default="mmcif", choices=["pdb", "mmcif"])
    group.add_argument("--recycling_steps", type=int, default=None, help="Override recycling steps. Defaults depend on refinement mode.")
    group.add_argument("--sampling_steps", type=int, default=None, help="Override sampling steps. Defaults depend on refinement mode.")
    group.add_argument("--diffusion_samples", type=int, default=None, help="Override diffusion sample count. Defaults depend on refinement mode.")
    group.add_argument("--max_parallel_samples", type=int, default=1)
    group.add_argument("--step_scale", type=float, default=None, help="Override the diffusion step scale. Defaults depend on the mode / execution plan.")
    group.add_argument("--no_kernels", action="store_true")
    group.add_argument("--seed", type=int, default=None)
    group.add_argument("--trainer_precision", type=str, default="32", help="Lightning trainer precision for scoring (default: 32).")
    group.add_argument("--work_dir", type=str, default=None, help="Optional work dir to keep processed intermediates")
    group.add_argument("--keep_work", action="store_true", help="Keep temporary work directory (default: delete)")


def _add_refinement_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("Refinement")
    group.add_argument("--structure_refine", action="store_true", help="Enable diffusion structure refinement before confidence scoring.")
    group.add_argument("--anchored_refine", action="store_true", help="Build ligand-pocket inference constraints and enable Boltz2 contact-guided refinement.")
    group.add_argument("--reference_from_input", action="store_true", help="Use input coordinates as Boltz2 reference geometry during structure refinement.")
    group.add_argument("--sampling_init_from_input", action="store_true", help="Initialize Boltz2 diffusion refinement from the input coordinates instead of pure noise. High-level modes enable this by default.")
    group.add_argument("--self_template", action="store_true", help="Inject the input protein structure as a forced Boltz2 template during structure refinement. High-level modes enable this by default.")
    group.add_argument("--self_template_threshold", type=float, default=2.0, help="Flat-bottom threshold in angstroms used by the forced self-template.")
    group.add_argument("--use_potentials", action="store_true", help="Enable official Boltz2 potentials path (fk_steering + physical_guidance_update).")
    group.add_argument("--template_exclude_pocket_margin", type=int, default=2, help="When --self_template is enabled with anchored refinement, exclude pocket residues +/- this many sequence positions from the forced template.")
    group.add_argument("--input_init_noise_scale", type=float, default=0.0, help="Relative noise scale added to input coordinates when --sampling_init_from_input is enabled.")
    group.add_argument("--sigma_max", type=float, default=None, help="Override Boltz2 diffusion sigma_max during structure refinement.")
    group.add_argument("--noise_scale", type=float, default=None, help="Override Boltz2 diffusion noise_scale during structure refinement.")
    group.add_argument("--gamma_0", type=float, default=None, help="Override Boltz2 diffusion gamma_0 during structure refinement.")
    group.add_argument("--gamma_min", type=float, default=None, help="Override Boltz2 diffusion gamma_min during structure refinement.")
    group.add_argument("--anchor_contact_cutoff", type=float, default=5.0, help="Select pocket residues within this heavy-atom distance of the input ligand pose.")
    group.add_argument("--anchor_max_distance", type=float, default=None, help="Contact upper bound used by anchored refinement guidance. Defaults depend on the mode / pocket.")
    group.add_argument("--anchor_max_residues", type=int, default=16, help="Maximum number of closest pocket residues to constrain during anchored refinement.")
    group.add_argument("--pose_anchor_atoms", type=int, default=4, help="Number of ligand heavy-atom anchors used to preserve the input pose orientation during anchored refinement.")
    group.add_argument("--pose_anchor_slack", type=float, default=0.75, help="Extra slack in angstroms added to each pose-preserving atom-level contact constraint.")
    group.add_argument("--anchor_strategy", type=str, default="pocket_only", choices=["hybrid", "atom_only", "pocket_only"], help="Anchoring strategy for guided refinement. For ligand geometry safety, high-level runs currently apply pocket-level constraints even if atom_only/hybrid is requested.")
    group.add_argument("--no_structure_refine", action="store_true", help=argparse.SUPPRESS)


def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("Outputs")
    group.add_argument("--ligand_smiles_map", type=str, default=None, help="Optional JSON map of ligand chain (or 'chain:resname') to SMILES for topology override.")
    group.add_argument("--compute_ipsae", action="store_true", help="Compute ligand-aware IPSAE metrics and append them to confidence JSON outputs.")
    group.add_argument("--ipsae_pae_cutoff", type=float, default=12.0, help="PAE cutoff used by ligand-aware IPSAE.")
    group.add_argument("--ipsae_dist_cutoff", type=float, default=5.0, help="Distance cutoff in angstroms used by ligand-aware IPSAE.")
    group.add_argument("--compute_interactions", action="store_true", help="Analyze protein-ligand interactions (PLIP) on the best-scoring structure and write interactions_<record>.json.")


def _add_affinity_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("Affinity")
    group.add_argument("--target_chain", type=str, default=None, help="Target protein chain ID(s), comma-separated. Required with --enable_affinity.")
    group.add_argument("--ligand_chain", type=str, default=None, help="Ligand chain ID(s), comma-separated. Required with --enable_affinity.")
    group.add_argument("--affinity_refine", action="store_true", help="When --enable_affinity is set, run diffusion refinement before affinity (higher quality, slower).")
    group.add_argument("--affinity_recycling_steps", type=int, default=1, help="Pairformer recycling steps for the affinity head. Default: 1 (ablation-optimized: matches upstream R=5 quality at ~10%% lower cost). Set to 5 for bit-for-bit upstream parity.")
    group.add_argument("--enable_affinity", action="store_true", help="Enable affinity prediction. Requires both --target_chain and --ligand_chain. Only supported for protein-small-molecule complexes.")


def _add_msa_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("MSA")
    group.add_argument("--use_msa_server", action="store_true", help="Enable external MSA generation for protein chains during input preparation.")
    group.add_argument("--msa_server_url", type=str, default=os.environ.get("MSA_SERVER_URL", "https://api.colabfold.com"), help="MSA server URL used when --use_msa_server is enabled.")
    group.add_argument("--msa_pairing_strategy", type=str, default="greedy", help="MSA pairing strategy for multi-protein inputs (default: greedy).")
    group.add_argument("--max_msa_seqs", type=int, default=8192, help="Maximum number of MSA sequences to keep per protein chain.")


def _add_docking_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("Docking (dock mode)")
    group.add_argument("--ligand_smiles", type=str, default=None,
                       help="SMILES string for dock mode (generates a 3-D conformer automatically).")
    group.add_argument("--ligand_smiles_file", type=str, default=None,
                       help="File containing SMILES for dock mode. .smi (tab-sep SMILES+name) or .csv (with SMILES column).")
    # Pocket center
    group.add_argument("--center_x", type=float, default=None,
                       help="X coordinate of the pocket center.")
    group.add_argument("--center_y", type=float, default=None,
                       help="Y coordinate of the pocket center.")
    group.add_argument("--center_z", type=float, default=None,
                       help="Z coordinate of the pocket center.")
    group.add_argument("--pocket_center", type=str, default=None,
                       help="Shorthand for --center_x/y/z as 'x,y,z' (e.g. '10.5,20.3,-5.0').")
    group.add_argument("--pocket_ligand", type=str, default=None,
                       help="Reference ligand file (.sdf/.mol2/.pdb) defining the pocket center.")
    group.add_argument("--pocket_residues", type=str, default=None,
                       help="Pocket residues 'CHAIN:RESNUM,...' (e.g. 'A:100,A:101,A:102'). Uses their Cα centroid as pocket center.")
    # Pocket size
    group.add_argument("--size_x", type=float, default=None,
                       help="Pocket box size in X (Å).")
    group.add_argument("--size_y", type=float, default=None,
                       help="Pocket box size in Y (Å).")
    group.add_argument("--size_z", type=float, default=None,
                       help="Pocket box size in Z (Å).")
    group.add_argument("--pocket_radius", type=float, default=7.0,
                       help="Pocket search radius in angstroms (default: 7.0). Ignored when --size_x/y/z are set.")
    group.add_argument("--dock_seed", type=int, default=42,
                       help="Random seed for ETKDG conformer generation (default: 42).")
    group.add_argument("--dock_poses", type=int, default=1,
                       help="Number of diverse initial pose candidates per SMILES (dock mode). "
                            ">1 runs a pose ensemble: each candidate is refined and the best-scored "
                            "pose per ligand is kept (default: 1; use 8-16 for hard targets).")


def build_main_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Boltz2Score scoring, refinement, or flexible docking on a protein-ligand complex."
    )
    _add_input_arguments(parser)
    _add_runtime_arguments(parser)
    _add_refinement_arguments(parser)
    _add_output_arguments(parser)
    _add_affinity_arguments(parser)
    _add_msa_arguments(parser)
    _add_docking_arguments(parser)
    return parser


def normalize_main_args(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> argparse.Namespace:
    try:
        args.mode = normalize_mode_name(args.mode)
    except ValueError as exc:
        parser.error(str(exc))
    if args.accelerator is None:
        # Deferred import: high-level modes (dock/pose/refine/interface) only
        # spawn a score-mode subprocess and never touch torch themselves.
        import torch

        args.accelerator = "gpu" if torch.cuda.is_available() else "cpu"
    return args


def _validate_main_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[bool, bool]:
    if args.structure_refine and args.no_structure_refine:
        raise ValueError("Cannot set both --structure_refine and --no_structure_refine.")
    if args.anchored_refine and not args.structure_refine:
        raise ValueError("--anchored_refine requires --structure_refine.")
    if args.anchor_contact_cutoff <= 0:
        raise ValueError("--anchor_contact_cutoff must be positive.")
    if args.anchor_max_distance is not None and args.anchor_max_distance <= 0:
        raise ValueError("--anchor_max_distance must be positive.")
    if args.anchor_max_residues <= 0:
        raise ValueError("--anchor_max_residues must be positive.")
    if args.pose_anchor_atoms < 0:
        raise ValueError("--pose_anchor_atoms must be non-negative.")
    if args.pose_anchor_slack < 0:
        raise ValueError("--pose_anchor_slack must be non-negative.")
    if args.noise_scale is not None and args.noise_scale <= 0:
        raise ValueError("--noise_scale must be positive.")
    if args.gamma_0 is not None and args.gamma_0 < 0:
        raise ValueError("--gamma_0 must be non-negative.")
    if args.gamma_min is not None and args.gamma_min < 0:
        raise ValueError("--gamma_min must be non-negative.")
    if args.input_init_noise_scale < 0:
        raise ValueError("--input_init_noise_scale must be non-negative.")
    if args.self_template_threshold <= 0:
        raise ValueError("--self_template_threshold must be positive.")
    if args.template_exclude_pocket_margin < 0:
        raise ValueError("--template_exclude_pocket_margin must be non-negative.")
    if args.sigma_max is not None and args.sigma_max <= 0:
        raise ValueError("--sigma_max must be positive.")
    if args.affinity_refine and not args.enable_affinity:
        raise ValueError("--affinity_refine requires --enable_affinity.")

    has_input = args.input is not None
    has_separate = args.protein_file is not None and args.ligand_file is not None

    # Dock mode has its own input contract: protein + SMILES + pocket definition.
    if args.mode == DOCK_MODE:
        return _validate_dock_args(args, parser)

    if not has_input and not has_separate:
        parser.error("Either --input or both --protein_file and --ligand_file must be provided")
    if has_input and has_separate:
        parser.error("Cannot use both --input and separate --protein_file/--ligand_file options")
    if args.protein_file and not args.ligand_file:
        parser.error("--ligand_file is required when using --protein_file")
    if args.ligand_file and not args.protein_file:
        parser.error("--protein_file is required when using --ligand_file")
    if args.ligand_indices and not has_separate:
        parser.error("--ligand_indices requires separate-input mode with --protein_file and --ligand_file")
    _parse_ligand_index_selection(args.ligand_indices)
    return has_input, has_separate


def _validate_dock_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[bool, bool]:
    """Validate the input contract for dock mode.

    Required: ``--protein_file`` + (``--ligand_smiles`` or ``--ligand_smiles_file``)
              + exactly one pocket definition (``--pocket_ligand``, ``--pocket_center``, or ``--pocket_residues``).
    """
    if args.input is not None:
        parser.error("dock mode does not support --input. Use --protein_file + --ligand_smiles.")
    if args.protein_file is None:
        parser.error("dock mode requires --protein_file.")
    if args.ligand_file is not None:
        parser.error("dock mode does not accept --ligand_file (use --ligand_smiles or --ligand_smiles_file instead).")
    if args.ligand_indices:
        parser.error("--ligand_indices is not supported in dock mode.")

    has_smiles = args.ligand_smiles is not None
    has_smiles_file = args.ligand_smiles_file is not None
    if not has_smiles and not has_smiles_file:
        parser.error("dock mode requires --ligand_smiles or --ligand_smiles_file.")
    if has_smiles and has_smiles_file:
        parser.error("Provide either --ligand_smiles or --ligand_smiles_file, not both.")

    # Pocket center: exactly one method.
    has_center_axes = args.center_x is not None or args.center_y is not None or args.center_z is not None
    center_axes_complete = args.center_x is not None and args.center_y is not None and args.center_z is not None
    if has_center_axes and not center_axes_complete:
        parser.error("--center_x/y/z must all be provided together.")
    pocket_defs = [
        center_axes_complete,
        args.pocket_center is not None,
        args.pocket_ligand is not None,
        args.pocket_residues is not None,
    ]
    count = sum(pocket_defs)
    if count == 0:
        parser.error(
            "dock mode requires a pocket definition. "
            "Provide one of: --center_x/--center_y/--center_z, --pocket_center, "
            "--pocket_ligand, or --pocket_residues."
        )
    if count > 1:
        parser.error(
            "Provide exactly one pocket definition method "
            "(--center_x/y/z, --pocket_center, --pocket_ligand, or --pocket_residues)."
        )

    # Pocket size: either all three size axes or none.
    has_size = args.size_x is not None or args.size_y is not None or args.size_z is not None
    if has_size and not (args.size_x is not None and args.size_y is not None and args.size_z is not None):
        parser.error("--size_x/y/z must all be provided together.")
    if has_size:
        if args.size_x <= 0 or args.size_y <= 0 or args.size_z <= 0:
            parser.error("--size_x/y/z must be positive.")
    elif args.pocket_radius <= 0:
        parser.error("--pocket_radius must be positive.")

    if int(getattr(args, "dock_poses", 1) or 1) < 1:
        parser.error("--dock_poses must be at least 1.")
    return False, True


def _resolve_sampling_defaults(args: argparse.Namespace) -> tuple[bool, int, int, int]:
    structure_refine = bool(args.structure_refine and not args.no_structure_refine)
    if structure_refine:
        return (
            structure_refine,
            args.recycling_steps if args.recycling_steps is not None else 3,
            args.sampling_steps if args.sampling_steps is not None else 200,
            args.diffusion_samples if args.diffusion_samples is not None else 5,
        )
    # Score mode skips diffusion entirely (structure is taken from the input),
    # so the expensive Pairformer recycling has almost no effect on the
    # confidence head output.  Empirically recycling_steps=1 gives scores within
    # ~0.5% of recycling_steps=20 while being ~5x faster on the forward pass.
    # Users who need bit-for-bit parity with full Boltz2 can still pass
    # --recycling_steps 20 explicitly.
    return (
        structure_refine,
        args.recycling_steps if args.recycling_steps is not None else 1,
        args.sampling_steps if args.sampling_steps is not None else 1,
        args.diffusion_samples if args.diffusion_samples is not None else 1,
    )


def _resolve_affinity_plan(args: argparse.Namespace) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    target_chains = tuple(_parse_chain_list(args.target_chain))
    ligand_chains = tuple(_parse_chain_list(args.ligand_chain))
    enable_affinity = bool(args.enable_affinity)

    if enable_affinity and (not target_chains or not ligand_chains):
        raise ValueError(
            "Affinity requires both --target_chain and --ligand_chain. "
            "Use both flags or omit --enable_affinity."
        )
    if enable_affinity and set(target_chains) & set(ligand_chains):
        raise ValueError("Target and ligand chains must be different.")
    return target_chains, ligand_chains, enable_affinity


def _build_job_specs(
    args: argparse.Namespace,
    *,
    has_separate: bool,
    load_ligand_entries: Callable[[Path], list[dict[str, object]]],
    slugify_identifier: Callable[[str, str], str],
) -> tuple[JobSpec, ...]:
    jobs: list[JobSpec] = []
    if has_separate:
        protein_path = Path(args.protein_file).expanduser().resolve()
        ligand_path = Path(args.ligand_file).expanduser().resolve()
        if not protein_path.exists():
            raise FileNotFoundError(f"Protein file not found: {protein_path}")
        if not ligand_path.exists():
            raise FileNotFoundError(f"Ligand file not found: {ligand_path}")

        ligand_entries = load_ligand_entries(ligand_path)
        selected_ligand_indices = set(_parse_ligand_index_selection(args.ligand_indices))
        if selected_ligand_indices:
            ligand_entries = [
                entry
                for entry in ligand_entries
                if int(entry.get("source_index") or 0) in selected_ligand_indices
            ]
            found_indices = {
                int(entry.get("source_index") or 0)
                for entry in ligand_entries
            }
            missing_indices = sorted(selected_ligand_indices - found_indices)
            if missing_indices:
                raise ValueError(
                    "Requested --ligand_indices not found in ligand file: "
                    + ",".join(str(idx) for idx in missing_indices)
                )
            print(
                "[Info] Filtered ligand entries by --ligand_indices: "
                + ",".join(str(idx) for idx in sorted(found_indices))
            )
        protein_prefix = slugify_identifier(protein_path.stem, "protein")
        used_record_ids: set[str] = set()
        for entry in ligand_entries:
            ligand_index = int(entry.get("source_index") or 0)
            ligand_label = slugify_identifier(
                str(entry.get("label") or f"ligand_{ligand_index:04d}"),
                f"ligand_{ligand_index:04d}",
            )
            base_record_id = f"{protein_prefix}__{ligand_index:04d}_{ligand_label}"
            record_id = base_record_id
            disambiguator = 2
            while record_id in used_record_ids:
                record_id = f"{base_record_id}_{disambiguator}"
                disambiguator += 1
            used_record_ids.add(record_id)
            jobs.append(
                JobSpec(
                    record_id=record_id,
                    input_path=None,
                    protein_path=protein_path,
                    ligand_entry=entry,
                )
            )
        return tuple(jobs)

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")
    return (
        JobSpec(
            record_id=slugify_identifier(input_path.stem, input_path.stem),
            input_path=input_path,
            protein_path=None,
            ligand_entry=None,
        ),
    )


def build_execution_plan(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    load_ligand_entries: Callable[[Path], list[dict[str, object]]],
    slugify_identifier: Callable[[str, str], str],
) -> ExecutionPlan:
    has_input, has_separate = _validate_main_args(args, parser)
    del has_input
    (
        structure_refine,
        resolved_recycling_steps,
        resolved_sampling_steps,
        resolved_diffusion_samples,
    ) = _resolve_sampling_defaults(args)
    target_chains, ligand_chains, run_affinity = _resolve_affinity_plan(args)

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Deferred import: pulls in torch + the boltz stack (~7s); only the
    # score-mode path needs it.
    from boltz.main import get_cache_path

    cache_dir = Path(args.cache or get_cache_path()).expanduser().resolve()

    resolved_output_format = args.output_format
    if args.compute_ipsae and resolved_output_format != "mmcif":
        print("[Warning] IPSAE requires mmCIF output; overriding --output_format to mmcif.")
        resolved_output_format = "mmcif"

    if args.work_dir:
        root_work_dir = Path(args.work_dir).expanduser().resolve()
        root_work_dir.mkdir(parents=True, exist_ok=True)
        cleanup_root = False
    else:
        root_work_dir = Path(tempfile.mkdtemp(prefix="boltz2score_", dir=output_dir))
        cleanup_root = not args.keep_work

    try:
        jobs = _build_job_specs(
            args,
            has_separate=has_separate,
            load_ligand_entries=load_ligand_entries,
            slugify_identifier=slugify_identifier,
        )
        ligand_smiles_map = _parse_ligand_smiles_map(args.ligand_smiles_map)
    except Exception:
        if cleanup_root:
            shutil.rmtree(root_work_dir, ignore_errors=True)
        raise

    return ExecutionPlan(
        output_dir=output_dir,
        cache_dir=cache_dir,
        root_work_dir=root_work_dir,
        cleanup_root=cleanup_root,
        jobs=jobs,
        ligand_smiles_map=ligand_smiles_map,
        target_chains=target_chains,
        ligand_chains=ligand_chains,
        run_affinity=run_affinity,
        structure_refine=structure_refine,
        resolved_recycling_steps=resolved_recycling_steps,
        resolved_sampling_steps=resolved_sampling_steps,
        resolved_diffusion_samples=resolved_diffusion_samples,
        resolved_output_format=resolved_output_format,
    )
