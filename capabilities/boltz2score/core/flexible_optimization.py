from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from core.modes import DOCK_MODE, INTERFACE_MODE, POSE_MODE, REFINE_MODE
from utils.result_utils import (
    discover_record_dirs,
    resolve_ipsae_file,
    resolve_structure_file,
    select_confidence_file_from_dir,
)


MODE_CONFIGS: dict[str, dict[str, object]] = {
    POSE_MODE: {
        "name": "pose_default",
        "sigma_max": 0.02,
        "sampling_steps": 8,
        "step_scale": 1.5,
        "anchor_max_distance": 5.5,
        "diffusion_samples": 5,
        "input_init_noise_scale": 0.0,
    },
    REFINE_MODE: {
        "name": "refine_default",
        "sigma_max": 0.03,
        "sampling_steps": 10,
        "step_scale": 1.2,
        "anchor_max_distance": 6.0,
        "diffusion_samples": 5,
        "input_init_noise_scale": 0.0,
    },
    INTERFACE_MODE: {
        "name": "interface_default",
        "sigma_max": 0.04,
        "sampling_steps": 12,
        "step_scale": 1.0,
        "anchor_max_distance": 6.5,
        "diffusion_samples": 5,
        "input_init_noise_scale": 0.0,
    },
    # Docking is generation, not refinement: the full de-novo noise
    # schedule re-poses the ligand conditioned on the input protein and the
    # pocket contact guidance.  The 0.02-0.05 sigma ladder above polishes an
    # existing pose and cannot escape a bad initial placement (see
    # docs/docking.md for the CDK2/CDK8 validation numbers).
    DOCK_MODE: {
        "name": "dock_default",
        "sigma_max": 160.0,
        "sampling_steps": 200,
        "step_scale": 1.0,
        # anchor_max_distance is derived from the resolved pocket radius
        # (cutoff + 1.0 A slack) in the dock pipeline, not fixed here.
        "diffusion_samples": 16,
        "input_init_noise_scale": 0.0,
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
        str(int(args.sampling_steps) if args.sampling_steps is not None else int(config["sampling_steps"])),
        "--diffusion_samples",
        str(int(args.diffusion_samples) if args.diffusion_samples is not None else int(config["diffusion_samples"])),
        "--step_scale",
        str(float(args.step_scale) if args.step_scale is not None else float(config["step_scale"])),
        "--trainer_precision",
        str(args.trainer_precision),
        "--structure_refine",
        "--anchor_contact_cutoff",
        str(args.anchor_contact_cutoff),
        "--anchor_max_distance",
        str(float(args.anchor_max_distance) if args.anchor_max_distance is not None else float(config["anchor_max_distance"])),
        "--anchor_max_residues",
        str(args.anchor_max_residues),
        "--pose_anchor_atoms",
        str(args.pose_anchor_atoms),
        "--pose_anchor_slack",
        str(args.pose_anchor_slack),
        "--anchor_strategy",
        str(args.anchor_strategy),
        "--input_init_noise_scale",
        str(args.input_init_noise_scale
            if args.input_init_noise_scale is not None
            else config["input_init_noise_scale"]),
        "--sigma_max",
        str(float(args.sigma_max) if args.sigma_max is not None else float(config["sigma_max"])),
    ]
    _append_cli_flag(cmd, "--no_kernels", args.no_kernels)
    _append_cli_arg(cmd, "--affinity_recycling_steps", args.affinity_recycling_steps)
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
    _append_cli_flag(cmd, "--compute_interactions", args.compute_interactions)
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
            shutil.rmtree(stale_path)
        elif stale_path.exists():
            stale_path.unlink()
    for record_dir in discover_record_dirs(output_dir).values():
        shutil.rmtree(record_dir)


def _select_dock_ensemble_winners(output_dir: Path) -> None:
    """Collapse the dock pose ensemble to one record per input ligand.

    With ``--dock_poses > 1`` every initial placement is scored as its own
    record named ``<ligand>__poseNN``.  Poses of one ligand are ranked by
    the interface-aware score from their rerank summary; losing records are
    deleted so the archive holds exactly one docked pose per ligand.  The
    full ranking is written to ``dock_ensemble_selection.json``.
    """
    pose_groups: dict[str, list[Path]] = {}
    for record_dir in sorted(set(discover_record_dirs(output_dir).values())):
        match = re.match(r"^(?P<ligand>.*?)__pose\d{2,}$", record_dir.name)
        if match:
            pose_groups.setdefault(match.group("ligand"), []).append(record_dir)
    if not pose_groups:
        return

    def _pose_score(record_dir: Path) -> dict[str, object]:
        summary_path = record_dir / f"best_sample_{record_dir.name}.json"
        if not summary_path.exists():
            raise FileNotFoundError(
                f"dock pose record is missing its rerank summary: {summary_path}")
        payload = json.loads(summary_path.read_text())
        selected = payload.get("selected_model")
        row = next((m for m in payload.get("models", [])
                    if m.get("model_stem") == selected), None)
        if row is None:
            raise ValueError(f"rerank summary has no selected model row: {summary_path}")
        return {"pose": record_dir.name, **row}

    summary: dict[str, object] = {}
    for ligand, record_dirs in sorted(pose_groups.items()):
        ranked = sorted((_pose_score(d) for d in record_dirs),
                        key=lambda r: r["interface_rank_score"], reverse=True)
        winner_name = ranked[0]["pose"]
        for record_dir in record_dirs:
            if record_dir.name != winner_name:
                shutil.rmtree(record_dir)
        summary[ligand] = {"winner": winner_name, "ranked_poses": ranked}
        print(f"[Dock] Pose ensemble {ligand!r}: kept {winner_name} "
              f"(score {ranked[0]['interface_rank_score']:.4f}), "
              f"pruned {len(record_dirs) - 1} pose record(s)")

    (output_dir / "dock_ensemble_selection.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def run_flexible_optimization(
    args: argparse.Namespace,
    output_dir: Path,
) -> None:
    config = built_in_config(str(args.mode))
    output_dir.mkdir(parents=True, exist_ok=True)
    _clear_output_dir(output_dir)
    print(f"[Info] Running flexible optimization config: {config['name']}")
    _run_trial(args, config, output_dir)
    _select_dock_ensemble_winners(output_dir)
    result_artifacts = _iter_result_artifacts(output_dir)
    if not result_artifacts:
        raise RuntimeError("Flexible optimization did not produce usable outputs.")
    for record_dir, conf_path, structure_path, ipsae_path in result_artifacts:
        _write_best_aliases(record_dir, conf_path, structure_path, ipsae_path)
    print(f"[Info] Flexible optimization written to {output_dir}")
