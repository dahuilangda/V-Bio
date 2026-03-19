from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from core.modes import INTERFACE_MODE, POSE_MODE, REFINE_MODE
from utils.result_utils import (
    discover_record_dirs,
    resolve_ipsae_file,
    resolve_structure_file,
    select_confidence_file_from_dir,
)


MODE_CONFIGS: dict[str, dict[str, object]] = {
    POSE_MODE: {
        "name": "pose_default",
        "sigma_max": 0.25,
        "sampling_steps": 8,
        "step_scale": 1.5,
        "anchor_max_distance": 5.5,
        "diffusion_samples": 5,
    },
    REFINE_MODE: {
        "name": "refine_default",
        "sigma_max": 0.35,
        "sampling_steps": 10,
        "step_scale": 1.2,
        "anchor_max_distance": 6.0,
        "diffusion_samples": 5,
    },
    INTERFACE_MODE: {
        "name": "interface_default",
        "sigma_max": 0.45,
        "sampling_steps": 12,
        "step_scale": 1.0,
        "anchor_max_distance": 6.5,
        "diffusion_samples": 5,
    },
}


def built_in_config(mode_name: str) -> dict[str, object]:
    try:
        return MODE_CONFIGS[mode_name]
    except KeyError as exc:
        raise ValueError(f"Flexible optimization is not supported for mode {mode_name!r}.") from exc


def _append_cli_arg(cmd: list[str], flag: str, value: object | None) -> None:
    if value is None:
        return
    cmd.extend([flag, str(value)])


def _append_cli_flag(cmd: list[str], flag: str, enabled: bool) -> None:
    if enabled:
        cmd.append(flag)


def _build_trial_command(
    args: argparse.Namespace,
    config: dict[str, object],
    trial_dir: Path,
) -> list[str]:
    repo_root = Path(__file__).resolve().parents[1]
    cmd = [
        sys.executable,
        str((repo_root / "boltz2score.py").resolve()),
        "--mode",
        "score",
        "--protein_file",
        str(Path(args.protein_file).expanduser().resolve()),
        "--ligand_file",
        str(Path(args.ligand_file).expanduser().resolve()),
        "--output_dir",
        str(trial_dir),
        "--output_format",
        str(args.output_format),
        "--devices",
        str(args.devices),
        "--accelerator",
        str(args.accelerator),
        "--num_workers",
        str(args.num_workers),
        "--max_parallel_samples",
        str(args.max_parallel_samples),
        "--recycling_steps",
        str(args.recycling_steps if args.recycling_steps is not None else 3),
        "--sampling_steps",
        str(int(config.get("sampling_steps", args.sampling_steps if args.sampling_steps is not None else 12))),
        "--diffusion_samples",
        str(int(config.get("diffusion_samples", args.diffusion_samples if args.diffusion_samples is not None else 1))),
        "--step_scale",
        str(float(config.get("step_scale", args.step_scale))),
        "--trainer_precision",
        str(args.trainer_precision),
        "--structure_refine",
        "--anchor_contact_cutoff",
        str(args.anchor_contact_cutoff),
        "--anchor_max_distance",
        str(float(config.get("anchor_max_distance", args.anchor_max_distance))),
        "--anchor_max_residues",
        str(args.anchor_max_residues),
        "--pose_anchor_atoms",
        str(args.pose_anchor_atoms),
        "--pose_anchor_slack",
        str(args.pose_anchor_slack),
        "--anchor_strategy",
        str(args.anchor_strategy),
        "--input_init_noise_scale",
        str(args.input_init_noise_scale),
        "--sigma_max",
        str(float(config.get("sigma_max", args.sigma_max if args.sigma_max is not None else 0.5))),
    ]
    _append_cli_arg(cmd, "--cache", args.cache)
    _append_cli_arg(cmd, "--checkpoint", args.checkpoint)
    _append_cli_arg(cmd, "--seed", args.seed)
    _append_cli_arg(cmd, "--work_dir", args.work_dir)
    _append_cli_arg(cmd, "--target_chain", args.target_chain)
    _append_cli_arg(cmd, "--ligand_chain", args.ligand_chain)
    _append_cli_arg(cmd, "--ligand_indices", args.ligand_indices)
    _append_cli_arg(cmd, "--ligand_smiles_map", args.ligand_smiles_map)
    _append_cli_arg(cmd, "--ipsae_pae_cutoff", args.ipsae_pae_cutoff)
    _append_cli_arg(cmd, "--ipsae_dist_cutoff", args.ipsae_dist_cutoff)
    _append_cli_arg(cmd, "--msa_server_url", args.msa_server_url)
    _append_cli_arg(cmd, "--msa_pairing_strategy", args.msa_pairing_strategy)
    _append_cli_arg(cmd, "--max_msa_seqs", args.max_msa_seqs)
    _append_cli_arg(cmd, "--noise_scale", args.noise_scale)
    _append_cli_arg(cmd, "--gamma_0", args.gamma_0)
    _append_cli_arg(cmd, "--gamma_min", args.gamma_min)
    _append_cli_flag(cmd, "--compute_ipsae", args.compute_ipsae)
    _append_cli_flag(cmd, "--keep_work", args.keep_work)
    _append_cli_flag(cmd, "--enable_affinity", args.enable_affinity)
    _append_cli_flag(cmd, "--affinity_refine", args.affinity_refine)
    _append_cli_flag(cmd, "--use_msa_server", args.use_msa_server)
    cmd.append("--anchored_refine")
    cmd.append("--sampling_init_from_input")
    _append_cli_flag(cmd, "--reference_from_input", bool(args.reference_from_input))
    _append_cli_flag(cmd, "--use_potentials", args.use_potentials)
    cmd.append("--self_template")
    _append_cli_arg(cmd, "--self_template_threshold", args.self_template_threshold)
    _append_cli_arg(cmd, "--template_exclude_pocket_margin", args.template_exclude_pocket_margin)
    return cmd


def _run_trial(args: argparse.Namespace, config: dict[str, object], trial_dir: Path) -> None:
    env = dict(os.environ)
    env.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
    trial_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        _build_trial_command(args, config, trial_dir),
        check=True,
        env=env,
    )


def _iter_result_artifacts(root_dir: Path) -> list[tuple[Path, Path, Path, Path | None]]:
    artifacts: list[tuple[Path, Path, Path, Path | None]] = []
    for _, record_dir in discover_record_dirs(root_dir).items():
        conf_path = select_confidence_file_from_dir(record_dir, required=False)
        if conf_path is None:
            continue
        structure_path = resolve_structure_file(record_dir, conf_path)
        ipsae_path = resolve_ipsae_file(record_dir, conf_path)
        artifacts.append((record_dir, conf_path, structure_path, ipsae_path))
    return artifacts


def _write_best_aliases(
    dst_dir: Path,
    selected_conf_src: Path,
    selected_struct_src: Path,
    selected_ipsae_src: Path | None,
) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)

    selected_conf_dst = dst_dir / selected_conf_src.name
    selected_struct_dst = dst_dir / selected_struct_src.name
    best_conf_alias = dst_dir / "best_confidence.json"
    best_struct_alias = dst_dir / f"best_model{selected_struct_src.suffix}"

    if selected_conf_dst.exists():
        if selected_conf_dst != best_conf_alias:
            shutil.copy2(selected_conf_dst, best_conf_alias)
    if selected_struct_dst.exists():
        if selected_struct_dst != best_struct_alias:
            shutil.copy2(selected_struct_dst, best_struct_alias)
    if selected_ipsae_src is not None:
        selected_ipsae_dst = dst_dir / selected_ipsae_src.name
        if selected_ipsae_dst.exists():
            best_ipsae_alias = dst_dir / "best_ipsae.json"
            if selected_ipsae_dst != best_ipsae_alias:
                shutil.copy2(selected_ipsae_dst, best_ipsae_alias)


def _clear_output_dir(output_dir: Path) -> None:
    for stale_path in [
        output_dir / "trials",
        output_dir / "optimized",
        output_dir / "all_trials.csv",
        output_dir / "best_trials.csv",
        output_dir / "optimized_results.csv",
        output_dir / "report.md",
        output_dir / "optimization_metadata.json",
    ]:
        if stale_path.is_dir():
            shutil.rmtree(stale_path, ignore_errors=True)
        elif stale_path.exists():
            stale_path.unlink()
    for record_dir in discover_record_dirs(output_dir).values():
        shutil.rmtree(record_dir, ignore_errors=True)


def run_flexible_optimization(
    args: argparse.Namespace,
    output_dir: Path,
) -> None:
    config = built_in_config(str(args.mode))
    output_dir.mkdir(parents=True, exist_ok=True)
    _clear_output_dir(output_dir)
    print(f"[Info] Running flexible optimization config: {config['name']}")
    _run_trial(args, config, output_dir)
    result_artifacts = _iter_result_artifacts(output_dir)
    if not result_artifacts:
        raise RuntimeError("Flexible optimization did not produce usable outputs.")
    for record_dir, conf_path, structure_path, ipsae_path in result_artifacts:
        _write_best_aliases(record_dir, conf_path, structure_path, ipsae_path)
    print(f"[Info] Flexible optimization written to {output_dir}")
